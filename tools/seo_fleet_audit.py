#!/usr/bin/env python3
"""Audit canonical, robots, sitemap, and every sitemap URL for a public site fleet."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from typing import Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

USER_AGENT = "QuantAlchemy-SEOFleetAudit/1.0"
MAX_BODY_BYTES = 5 * 1024 * 1024
XML_CONTENT_TYPES = ("application/xml", "text/xml", "+xml")


@dataclass(frozen=True)
class SiteConfig:
    name: str
    origin: str
    repository: str = ""
    expected_text_paths: tuple[str, ...] = ()
    required_canonical_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        origin = self.origin.rstrip("/")
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                f"{self.name}: invalid origin port: {self.origin}"
            ) from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                f"{self.name}: origin must be an HTTPS origin without a path: {self.origin}"
            )
        object.__setattr__(self, "origin", origin)
        if self.repository and not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository
        ):
            raise ValueError(
                f"{self.name}: repository must use the GitHub owner/name format: "
                f"{self.repository}"
            )
        for field_name in ("expected_text_paths", "required_canonical_paths"):
            paths = tuple(dict.fromkeys(getattr(self, field_name)))
            for path in paths:
                parsed_path = urlsplit(path)
                if (
                    not path.startswith("/")
                    or path.startswith("//")
                    or parsed_path.scheme
                    or parsed_path.netloc
                    or parsed_path.query
                    or parsed_path.fragment
                ):
                    raise ValueError(
                        f"{self.name}: {field_name} entries must be root-relative paths "
                        f"without query or fragment: {path}"
                    )
            object.__setattr__(self, field_name, paths)


@dataclass(frozen=True)
class FetchReceipt:
    requested_url: str
    status: int
    final_url: str
    content_type: str
    body: str
    redirects: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class Finding:
    code: str
    url: str
    expected: str
    observed: str


@dataclass
class SiteAudit:
    site: SiteConfig
    findings: list[Finding] = field(default_factory=list)
    checked_urls: int = 0
    sitemap_urls: int = 0

    @property
    def healthy(self) -> bool:
        return not self.findings


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@lru_cache(maxsize=128)
def _resolve_addresses(hostname: str) -> tuple[str, ...]:
    addresses = {
        str(sockaddr[0])
        for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
            hostname, 443, type=socket.SOCK_STREAM
        )
    }
    return tuple(sorted(addresses))


def is_safe_public_https_url(
    url: str,
    resolver: Callable[[str], Iterable[str]] = _resolve_addresses,
) -> bool:
    """Reject non-HTTPS and non-public targets before any network request."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        return False
    try:
        literal_address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        return literal_address.is_global
    try:
        addresses = list(resolver(parsed.hostname))
        return bool(addresses) and all(
            ipaddress.ip_address(address).is_global for address in addresses
        )
    except (OSError, ValueError):
        return False


def fetch_url(url: str, timeout: float = 20.0, max_redirects: int = 5) -> FetchReceipt:
    """Fetch a URL while preserving every redirect as an evidence receipt."""
    opener = build_opener(_NoRedirect)
    requested_url = url
    current_url = url
    redirects: list[str] = []

    for _ in range(max_redirects + 1):
        if not is_safe_public_https_url(current_url):
            return FetchReceipt(
                requested_url=requested_url,
                status=0,
                final_url=current_url,
                content_type="",
                body="",
                redirects=tuple(redirects),
                error="unsafe or non-public HTTPS target",
            )
        request = Request(
            current_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xml,text/xml,text/plain,*/*;q=0.1",
            },
        )
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as exc:
            response = exc
        except Exception as exc:  # Network failures must become exact receipts.
            return FetchReceipt(
                requested_url=requested_url,
                status=0,
                final_url=current_url,
                content_type="",
                body="",
                redirects=tuple(redirects),
                error=f"{type(exc).__name__}: {exc}",
            )

        status = int(response.getcode() or 0)
        location = response.headers.get("Location")
        if status in {301, 302, 303, 307, 308} and location:
            current_url = urljoin(current_url, location)
            redirects.append(current_url)
            if len(redirects) > max_redirects:
                return FetchReceipt(
                    requested_url=requested_url,
                    status=status,
                    final_url=current_url,
                    content_type=response.headers.get("Content-Type", ""),
                    body="",
                    redirects=tuple(redirects),
                    error=f"redirect limit exceeded ({max_redirects})",
                )
            continue

        raw_body = response.read(MAX_BODY_BYTES + 1)
        if len(raw_body) > MAX_BODY_BYTES:
            return FetchReceipt(
                requested_url=requested_url,
                status=status,
                final_url=current_url,
                content_type=response.headers.get("Content-Type", ""),
                body="",
                redirects=tuple(redirects),
                error=f"response exceeded {MAX_BODY_BYTES} bytes",
            )
        charset = response.headers.get_content_charset() or "utf-8"
        return FetchReceipt(
            requested_url=requested_url,
            status=status,
            final_url=current_url,
            content_type=response.headers.get("Content-Type", ""),
            body=raw_body.decode(charset, errors="replace"),
            redirects=tuple(redirects),
        )

    raise AssertionError("unreachable")


class _CanonicalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.canonicals: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        rel_tokens = {token.lower() for token in values.get("rel", "").split()}
        if "canonical" in rel_tokens and values.get("href"):
            self.canonicals.append(values["href"])


def extract_canonicals(html: str) -> list[str]:
    parser = _CanonicalParser()
    parser.feed(html)
    return parser.canonicals


def _normalized_url(url: str) -> tuple[str, str, str, str]:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    return parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query


def urls_equivalent(left: str, right: str) -> bool:
    return _normalized_url(left) == _normalized_url(right)


def audit_repository_homepage(
    site: SiteConfig,
    homepage_fetcher: Callable[[str], str],
) -> list[Finding]:
    """Compare a repository's GitHub homepage with its canonical production origin."""
    if not site.repository:
        return []
    repository_url = f"https://github.com/{site.repository}"
    try:
        homepage = homepage_fetcher(site.repository).strip()
    except Exception as exc:
        return [
            Finding(
                "REPOSITORY_HOMEPAGE_LOOKUP_FAILED",
                repository_url,
                site.origin,
                f"{type(exc).__name__}: {exc}",
            )
        ]
    if not homepage:
        return [
            Finding(
                "REPOSITORY_HOMEPAGE_MISSING",
                repository_url,
                site.origin,
                "empty homepage",
            )
        ]
    if not urls_equivalent(homepage, site.origin):
        return [
            Finding(
                "REPOSITORY_HOMEPAGE_MISMATCH",
                repository_url,
                site.origin,
                homepage,
            )
        ]
    return []


def audit_repository_homepages(
    audits: Iterable[SiteAudit],
    homepage_fetcher: Callable[[str], str],
) -> bool:
    """Attach repository homepage findings and report lookup infrastructure failure."""
    lookup_failed = False
    for audit in audits:
        findings = audit_repository_homepage(audit.site, homepage_fetcher)
        audit.findings.extend(findings)
        lookup_failed = lookup_failed or any(
            finding.code == "REPOSITORY_HOMEPAGE_LOOKUP_FAILED" for finding in findings
        )
    return lookup_failed


def fetch_repository_homepage(repository: str) -> str:
    """Read a repository homepage through the authenticated GitHub CLI."""
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError(f"invalid GitHub repository: {repository}")
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("authenticated gh CLI is unavailable")
    try:
        response = subprocess.run(
            [gh, "api", f"repos/{repository}"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10.0,
        )
        if len(response.stdout.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError(f"GitHub response exceeded {MAX_BODY_BYTES} bytes")
        payload = json.loads(response.stdout)
    except Exception as exc:
        raise RuntimeError(
            f"GitHub repository lookup failed for {repository}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"GitHub repository lookup returned invalid data for {repository}"
        )
    homepage = payload.get("homepage")
    return homepage if isinstance(homepage, str) else ""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _is_html(receipt: FetchReceipt) -> bool:
    return "text/html" in receipt.content_type.lower()


def _is_xml(receipt: FetchReceipt) -> bool:
    content_type = receipt.content_type.lower()
    return any(kind in content_type for kind in XML_CONTENT_TYPES)


def _finding(
    audit: SiteAudit, code: str, url: str, expected: str, observed: str
) -> None:
    audit.findings.append(Finding(code, url, expected, observed))


def audit_site(
    site: SiteConfig,
    fetcher: Callable[[str], FetchReceipt] = fetch_url,
    *,
    max_workers: int = 1,
) -> SiteAudit:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    audit = SiteAudit(site=site)
    cache: dict[str, FetchReceipt] = {}
    cache_lock = Lock()

    def fetch(url: str) -> FetchReceipt:
        with cache_lock:
            cached = cache.get(url)
        if cached is not None:
            return cached
        try:
            receipt = fetcher(url)
        except Exception as exc:
            receipt = FetchReceipt(
                requested_url=url,
                status=0,
                final_url=url,
                content_type="",
                body="",
                error=f"{type(exc).__name__}: {exc}",
            )
        with cache_lock:
            return cache.setdefault(url, receipt)

    root_url = f"{site.origin}/"
    root = fetch(root_url)
    _check_http_receipt(audit, root, "ROOT", root_url)
    if root.status == 200:
        if _is_html(root):
            _check_canonical(audit, root, root_url, "ROOT")
        else:
            _finding(
                audit,
                "ROOT_CONTENT_TYPE",
                root_url,
                "text/html",
                root.content_type or "missing Content-Type",
            )

    for path in site.expected_text_paths:
        expected_url = f"{site.origin}{path}"
        text_receipt = fetch(expected_url)
        _check_http_receipt(audit, text_receipt, "EXPECTED_TEXT", expected_url)
        if (
            text_receipt.status == 200
            and "text/plain" not in text_receipt.content_type.lower()
        ):
            _finding(
                audit,
                "EXPECTED_TEXT_CONTENT_TYPE",
                expected_url,
                "text/plain",
                text_receipt.content_type or "missing Content-Type",
            )

    for path in site.required_canonical_paths:
        expected_url = f"{site.origin}{path}"
        page = fetch(expected_url)
        _check_http_receipt(audit, page, "REQUIRED_CANONICAL", expected_url)
        if page.status == 200:
            if _is_html(page):
                _check_canonical(
                    audit,
                    page,
                    expected_url,
                    "REQUIRED_CANONICAL",
                    required=True,
                )
            else:
                _finding(
                    audit,
                    "REQUIRED_CANONICAL_CONTENT_TYPE",
                    expected_url,
                    "text/html",
                    page.content_type or "missing Content-Type",
                )

    robots_url = f"{site.origin}/robots.txt"
    robots = fetch(robots_url)
    _check_http_receipt(audit, robots, "ROBOTS", robots_url)
    if robots.status == 200 and "text/plain" not in robots.content_type.lower():
        _finding(
            audit,
            "ROBOTS_CONTENT_TYPE",
            robots_url,
            "text/plain",
            robots.content_type or "missing Content-Type",
        )

    expected_sitemap = f"{site.origin}/sitemap.xml"
    sitemap_refs = re.findall(
        r"^\s*Sitemap\s*:\s*(\S+)\s*$", robots.body, flags=re.IGNORECASE | re.MULTILINE
    )
    if expected_sitemap not in sitemap_refs:
        _finding(
            audit,
            "ROBOTS_SITEMAP_MISSING",
            robots_url,
            expected_sitemap,
            ", ".join(sitemap_refs) if sitemap_refs else "no Sitemap directive",
        )

    sitemap_queue = list(dict.fromkeys([*sitemap_refs, expected_sitemap]))
    visited_sitemaps: set[str] = set()
    for sitemap_url in sitemap_queue:
        if _host(sitemap_url) != _host(site.origin):
            _finding(
                audit,
                "SITEMAP_REF_HOST_MISMATCH",
                sitemap_url,
                _host(site.origin),
                _host(sitemap_url) or "missing host",
            )
            continue
        _audit_sitemap(
            audit,
            sitemap_url,
            fetch,
            visited_sitemaps,
            canonical_host=_host(site.origin),
            max_workers=max_workers,
        )

    audit.checked_urls = len(cache)
    return audit


def _check_http_receipt(
    audit: SiteAudit, receipt: FetchReceipt, prefix: str, expected_url: str
) -> None:
    if receipt.status != 200:
        observed = f"HTTP {receipt.status}"
        if receipt.error:
            observed += f" ({receipt.error})"
        _finding(audit, f"{prefix}_HTTP_STATUS", expected_url, "HTTP 200", observed)
    if not urls_equivalent(receipt.final_url, expected_url):
        chain = " -> ".join((receipt.requested_url, *receipt.redirects))
        _finding(
            audit,
            f"{prefix}_FINAL_URL" if prefix != "LOC" else "LOC_REDIRECT",
            receipt.requested_url,
            expected_url,
            f"HTTP {receipt.status} -> `{receipt.final_url}`; chain: {chain}",
        )


def _check_canonical(
    audit: SiteAudit,
    receipt: FetchReceipt,
    expected_url: str,
    prefix: str,
    *,
    required: bool = False,
) -> None:
    canonicals = extract_canonicals(receipt.body)
    if not canonicals:
        if required:
            _finding(
                audit,
                f"{prefix}_MISSING",
                receipt.requested_url,
                expected_url,
                "no canonical link",
            )
        return
    if len(canonicals) != 1 or not urls_equivalent(canonicals[0], expected_url):
        _finding(
            audit,
            f"{prefix}_CANONICAL_MISMATCH",
            receipt.requested_url,
            expected_url,
            ", ".join(canonicals),
        )


def _audit_sitemap(
    audit: SiteAudit,
    sitemap_url: str,
    fetch: Callable[[str], FetchReceipt],
    visited_sitemaps: set[str],
    canonical_host: str,
    max_workers: int,
) -> None:
    if sitemap_url in visited_sitemaps:
        return
    visited_sitemaps.add(sitemap_url)

    receipt = fetch(sitemap_url)
    _check_http_receipt(audit, receipt, "SITEMAP", sitemap_url)
    if receipt.status != 200:
        return
    if not _is_xml(receipt):
        _finding(
            audit,
            "SITEMAP_CONTENT_TYPE",
            sitemap_url,
            "XML Content-Type",
            receipt.content_type or "missing Content-Type",
        )

    try:
        root = ET.fromstring(receipt.body)
    except ET.ParseError as exc:
        _finding(
            audit,
            "SITEMAP_XML_INVALID",
            sitemap_url,
            "parseable XML",
            str(exc),
        )
        return

    root_name = _local_name(root.tag)
    if root_name not in {"urlset", "sitemapindex"}:
        _finding(
            audit,
            "SITEMAP_ROOT_INVALID",
            sitemap_url,
            "<urlset> or <sitemapindex>",
            f"<{root_name}>",
        )
        return
    locs = [
        (element.text or "").strip()
        for element in root.iter()
        if _local_name(element.tag) == "loc" and (element.text or "").strip()
    ]
    if not locs:
        _finding(
            audit,
            "SITEMAP_EMPTY",
            sitemap_url,
            "at least one <loc>",
            "zero <loc> entries",
        )
        return

    if root_name == "sitemapindex":
        for nested_url in locs:
            if _host(nested_url) != canonical_host:
                _finding(
                    audit,
                    "SITEMAP_REF_HOST_MISMATCH",
                    nested_url,
                    canonical_host,
                    _host(nested_url) or "missing host",
                )
                continue
            _audit_sitemap(
                audit,
                nested_url,
                fetch,
                visited_sitemaps,
                canonical_host,
                max_workers,
            )
        return

    audit.sitemap_urls += len(locs)

    def audit_loc(loc: str) -> None:
        if _host(loc) != canonical_host:
            _finding(
                audit,
                "LOC_HOST_MISMATCH",
                loc,
                canonical_host,
                _host(loc) or "missing host",
            )
            return
        page = fetch(loc)
        _check_http_receipt(audit, page, "LOC", loc)
        if page.status == 200 and _is_html(page):
            _check_canonical(audit, page, loc, "LOC")

    if max_workers == 1:
        for loc in locs:
            audit_loc(loc)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(audit_loc, locs))


def render_markdown(audits: Iterable[SiteAudit]) -> str:
    audit_list = list(audits)
    finding_count = sum(len(audit.findings) for audit in audit_list)
    checked_count = sum(audit.checked_urls for audit in audit_list)
    sitemap_count = sum(audit.sitemap_urls for audit in audit_list)
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Weekly SEO Fleet Audit",
        "",
        f"Generated: `{generated}`",
        "",
        (
            f"**Summary:** {len(audit_list)} sites; {checked_count} fetched URLs; "
            f"{sitemap_count} sitemap page URLs; {finding_count} defects."
        ),
        "",
    ]

    for audit in audit_list:
        icon = "✅" if audit.healthy else "❌"
        repository = f" — `{audit.site.repository}`" if audit.site.repository else ""
        lines.extend(
            [
                f"## {icon} {audit.site.name}{repository}",
                "",
                (
                    f"Origin: `{audit.site.origin}` · fetched: {audit.checked_urls} · "
                    f"sitemap URLs: {audit.sitemap_urls}"
                ),
                "",
            ]
        )
        if audit.healthy:
            lines.extend(["No defects found.", ""])
            continue
        for finding in audit.findings:
            lines.append(
                f"- **{finding.code}** — `{finding.url}` — "
                f"expected: {finding.expected}; observed: {finding.observed}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _load_sites(path: Path) -> list[SiteConfig]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["sites"] if isinstance(payload, dict) else payload
    return [SiteConfig(**row) for row in rows]


def _json_payload(audits: list[SiteAudit]) -> dict[str, object]:
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "healthy": all(audit.healthy for audit in audits),
        "sites": [
            {
                "site": asdict(audit.site),
                "healthy": audit.healthy,
                "checkedUrls": audit.checked_urls,
                "sitemapUrls": audit.sitemap_urls,
                "findings": [asdict(finding) for finding in audit.findings],
            }
            for audit in audits
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/public-sites.json"))
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--max-workers", type=int, default=8)
    args = parser.parse_args(argv)

    audits = [
        audit_site(site, max_workers=args.max_workers)
        for site in _load_sites(args.config)
    ]
    homepage_lookup_failed = audit_repository_homepages(
        audits, fetch_repository_homepage
    )
    markdown = render_markdown(audits)
    print(markdown, end="")

    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(_json_payload(audits), indent=2) + "\n", encoding="utf-8"
        )

    if homepage_lookup_failed:
        return 2
    return 0 if all(audit.healthy for audit in audits) else 1


if __name__ == "__main__":
    sys.exit(main())
