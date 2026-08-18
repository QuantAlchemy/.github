from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import Mock, patch

import tools.seo_fleet_audit as seo_fleet_audit
from tools.seo_fleet_audit import (
    FetchReceipt,
    SiteConfig,
    audit_site,
    is_safe_public_https_url,
    render_markdown,
)


class FakeFetcher:
    def __init__(self, receipts: dict[str, FetchReceipt]):
        self.receipts = receipts
        self.calls: list[str] = []

    def __call__(self, url: str) -> FetchReceipt:
        self.calls.append(url)
        return self.receipts[url]


class SeoFleetAuditTests(unittest.TestCase):
    def test_repository_homepage_accepts_exact_origin_and_trailing_slash(self) -> None:
        site = SiteConfig(
            name="Example",
            origin="https://www.example.com",
            repository="Example/site",
        )

        for homepage in (
            "https://www.example.com",
            "https://www.example.com/",
        ):
            with self.subTest(homepage=homepage):
                findings = seo_fleet_audit.audit_repository_homepage(
                    site, lambda _repository: homepage
                )
                self.assertEqual([], findings)

        for homepage in (
            "http://www.example.com",
            "https://example.com",
            "https://WWW.example.com",
            "https://www.example.com/pricing",
            "https://www.example.com?preview=1",
            "https://www.example.com#preview",
            "https://www.example.com////",
        ):
            with self.subTest(homepage=homepage):
                findings = seo_fleet_audit.audit_repository_homepage(
                    site, lambda _repository: homepage
                )
                self.assertEqual(
                    ["REPOSITORY_HOMEPAGE_MISMATCH"],
                    [finding.code for finding in findings],
                )

    def test_repository_homepage_reports_missing_wrong_and_failed_lookups(self) -> None:
        site = SiteConfig(
            name="Example",
            origin="https://www.example.com",
            repository="Example/site",
        )

        missing = seo_fleet_audit.audit_repository_homepage(
            site, lambda _repository: ""
        )
        wrong = seo_fleet_audit.audit_repository_homepage(
            site, lambda _repository: "https://example-site.vercel.app"
        )

        def fail(_repository: str) -> str:
            raise RuntimeError("GitHub API unavailable")

        failed = seo_fleet_audit.audit_repository_homepage(site, fail)

        self.assertEqual(
            ["REPOSITORY_HOMEPAGE_MISSING"], [item.code for item in missing]
        )
        self.assertEqual(
            ["REPOSITORY_HOMEPAGE_MISMATCH"], [item.code for item in wrong]
        )
        self.assertEqual(
            ["REPOSITORY_HOMEPAGE_LOOKUP_FAILED"], [item.code for item in failed]
        )
        self.assertEqual("https://www.example.com", wrong[0].expected)
        self.assertEqual("https://example-site.vercel.app", wrong[0].observed)

    def test_repository_homepage_checks_join_findings_and_flag_lookup_failures(
        self,
    ) -> None:
        audits = [
            seo_fleet_audit.SiteAudit(
                SiteConfig(
                    name="Healthy",
                    origin="https://healthy.example.com",
                    repository="Example/healthy",
                )
            ),
            seo_fleet_audit.SiteAudit(
                SiteConfig(
                    name="Drifted",
                    origin="https://drifted.example.com",
                    repository="Example/drifted",
                )
            ),
            seo_fleet_audit.SiteAudit(
                SiteConfig(
                    name="Unavailable",
                    origin="https://unavailable.example.com",
                    repository="Example/unavailable",
                )
            ),
        ]

        def fetch(repository: str) -> str:
            if repository == "Example/healthy":
                return "https://healthy.example.com/"
            if repository == "Example/drifted":
                return "https://preview.example.com"
            raise RuntimeError("rate limited")

        operational_failure = seo_fleet_audit.audit_repository_homepages(audits, fetch)

        self.assertTrue(operational_failure)
        self.assertEqual([], audits[0].findings)
        self.assertEqual(
            ["REPOSITORY_HOMEPAGE_MISMATCH"],
            [finding.code for finding in audits[1].findings],
        )
        self.assertEqual(
            ["REPOSITORY_HOMEPAGE_LOOKUP_FAILED"],
            [finding.code for finding in audits[2].findings],
        )

    def test_repository_homepage_lookup_uses_authenticated_gh_cli(self) -> None:
        response = Mock(stdout='{"homepage":"https://www.example.com"}\n')

        with (
            patch("subprocess.run", return_value=response) as run,
            patch.object(
                seo_fleet_audit,
                "build_opener",
                side_effect=AssertionError("direct unauthenticated API call"),
            ),
            patch("shutil.which", return_value="/authenticated/gh"),
        ):
            homepage = seo_fleet_audit.fetch_repository_homepage("Example/site")

        self.assertEqual("https://www.example.com", homepage)
        run.assert_called_once_with(
            ["/authenticated/gh", "api", "repos/Example/site"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10.0,
        )

    def test_main_runs_repository_homepage_canary_and_preserves_operational_failure(
        self,
    ) -> None:
        site = SiteConfig(
            name="Example",
            origin="https://www.example.com",
            repository="Example/site",
        )
        audit = seo_fleet_audit.SiteAudit(site)

        with (
            patch.object(seo_fleet_audit, "_load_sites", return_value=[site]),
            patch.object(seo_fleet_audit, "audit_site", return_value=audit),
            patch.object(
                seo_fleet_audit,
                "audit_repository_homepages",
                return_value=True,
            ) as homepage_canary,
            patch.object(seo_fleet_audit, "render_markdown", return_value="receipt\n"),
            redirect_stdout(StringIO()),
        ):
            status = seo_fleet_audit.main(["--config", "unused.json"])

        homepage_canary.assert_called_once_with(
            [audit], seo_fleet_audit.fetch_repository_homepage
        )
        self.assertEqual(2, status)

    def test_main_preserves_repository_homepage_drift_as_defect_status(self) -> None:
        site = SiteConfig(
            name="Example",
            origin="https://www.example.com",
            repository="Example/site",
        )
        audit = seo_fleet_audit.SiteAudit(site)

        with (
            patch.object(seo_fleet_audit, "_load_sites", return_value=[site]),
            patch.object(seo_fleet_audit, "audit_site", return_value=audit),
            patch.object(
                seo_fleet_audit,
                "fetch_repository_homepage",
                return_value="https://preview.example.com",
            ),
            patch.object(seo_fleet_audit, "render_markdown", return_value="receipt\n"),
            redirect_stdout(StringIO()),
        ):
            status = seo_fleet_audit.main(["--config", "unused.json"])

        self.assertEqual(1, status)
        self.assertEqual(
            ["REPOSITORY_HOMEPAGE_MISMATCH"],
            [finding.code for finding in audit.findings],
        )

    def test_healthy_site_checks_root_robots_sitemap_and_every_loc(self) -> None:
        origin = "https://example.com"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="text/html; charset=utf-8",
                    body='<link rel="canonical" href="https://example.com/">',
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<?xml version="1.0"?>'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{origin}/</loc></url>"
                        f"<url><loc>{origin}/pricing</loc></url>"
                        "</urlset>"
                    ),
                ),
                f"{origin}/pricing": FetchReceipt(
                    requested_url=f"{origin}/pricing",
                    status=200,
                    final_url=f"{origin}/pricing",
                    content_type="text/html",
                    body="<html><title>Pricing</title></html>",
                ),
            }
        )

        audit = audit_site(
            SiteConfig(name="Example", origin=origin), fetch, max_workers=4
        )

        self.assertEqual([], audit.findings)
        self.assertEqual(4, audit.checked_urls)
        self.assertEqual(2, audit.sitemap_urls)

    def test_site_contract_reports_four_krong_artifact_defects(self) -> None:
        origin = "https://www.krong.ai"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="text/html",
                    body="<html><title>Krong AI</title></html>",
                ),
                f"{origin}/progress": FetchReceipt(
                    requested_url=f"{origin}/progress",
                    status=200,
                    final_url=f"{origin}/progress",
                    content_type="text/html",
                    body="<html><title>Progress</title></html>",
                ),
                f"{origin}/llms.txt": FetchReceipt(
                    requested_url=f"{origin}/llms.txt",
                    status=404,
                    final_url=f"{origin}/llms.txt",
                    content_type="text/html",
                    body="not found",
                ),
                f"{origin}/ai.txt": FetchReceipt(
                    requested_url=f"{origin}/ai.txt",
                    status=404,
                    final_url=f"{origin}/ai.txt",
                    content_type="text/html",
                    body="not found",
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"Sitemap: {origin}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{origin}/</loc></url>"
                        "</urlset>"
                    ),
                ),
            }
        )

        audit = audit_site(
            SiteConfig(
                name="Krong AI",
                origin=origin,
                expected_text_paths=("/llms.txt", "/ai.txt"),
                required_canonical_paths=("/", "/progress"),
            ),
            fetch,
        )
        receipts = {(finding.code, finding.url) for finding in audit.findings}

        self.assertEqual(
            {
                ("EXPECTED_TEXT_HTTP_STATUS", f"{origin}/llms.txt"),
                ("EXPECTED_TEXT_HTTP_STATUS", f"{origin}/ai.txt"),
                ("REQUIRED_CANONICAL_MISSING", f"{origin}/"),
                ("REQUIRED_CANONICAL_MISSING", f"{origin}/progress"),
            },
            receipts,
        )
        report = render_markdown([audit])
        for path in ("/", "/progress", "/llms.txt", "/ai.txt"):
            self.assertIn(f"`{origin}{path}`", report)

    def test_site_contract_accepts_expected_text_and_canonical_paths(self) -> None:
        origin = "https://example.com"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="text/html",
                    body=f'<link rel="canonical" href="{origin}/">',
                ),
                f"{origin}/progress": FetchReceipt(
                    requested_url=f"{origin}/progress",
                    status=200,
                    final_url=f"{origin}/progress",
                    content_type="text/html",
                    body=f'<link rel="canonical" href="{origin}/progress">',
                ),
                f"{origin}/llms.txt": FetchReceipt(
                    requested_url=f"{origin}/llms.txt",
                    status=200,
                    final_url=f"{origin}/llms.txt",
                    content_type="text/plain; charset=utf-8",
                    body="# Example",
                ),
                f"{origin}/ai.txt": FetchReceipt(
                    requested_url=f"{origin}/ai.txt",
                    status=200,
                    final_url=f"{origin}/ai.txt",
                    content_type="text/plain",
                    body="See /llms.txt",
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"Sitemap: {origin}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{origin}/</loc></url>"
                        f"<url><loc>{origin}/progress</loc></url>"
                        "</urlset>"
                    ),
                ),
            }
        )

        audit = audit_site(
            SiteConfig(
                name="Example",
                origin=origin,
                expected_text_paths=("/llms.txt", "/ai.txt"),
                required_canonical_paths=("/", "/progress"),
            ),
            fetch,
        )

        self.assertEqual([], audit.findings)
        self.assertEqual(6, audit.checked_urls)

    def test_reports_exact_wrong_host_redirect_and_broken_url_receipts(self) -> None:
        origin = "https://example.com"
        foreign = "https://preview.example.net"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="text/html",
                    body=f'<link rel="canonical" href="{foreign}/">',
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"Sitemap: {foreign}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{origin}/gone</loc></url>"
                        f"<url><loc>{origin}/moved</loc></url>"
                        f"<url><loc>{foreign}/leaked</loc></url>"
                        "</urlset>"
                    ),
                ),
                f"{origin}/gone": FetchReceipt(
                    requested_url=f"{origin}/gone",
                    status=404,
                    final_url=f"{origin}/gone",
                    content_type="text/html",
                    body="not found",
                ),
                f"{origin}/moved": FetchReceipt(
                    requested_url=f"{origin}/moved",
                    status=200,
                    final_url=f"{origin}/pricing",
                    content_type="text/html",
                    body="moved",
                    redirects=(f"{origin}/pricing",),
                ),
            }
        )

        audit = audit_site(SiteConfig(name="Example", origin=origin), fetch)
        codes = {finding.code for finding in audit.findings}

        self.assertIn("ROOT_CANONICAL_MISMATCH", codes)
        self.assertIn("ROBOTS_SITEMAP_MISSING", codes)
        self.assertIn("SITEMAP_REF_HOST_MISMATCH", codes)
        self.assertIn("LOC_HTTP_STATUS", codes)
        self.assertIn("LOC_HOST_MISMATCH", codes)
        self.assertIn("LOC_REDIRECT", codes)

        report = render_markdown([audit])
        self.assertIn(f"`{origin}/gone`", report)
        self.assertIn("HTTP 200", report)
        self.assertIn("HTTP 404", report)
        self.assertIn(f"`{origin}/pricing`", report)
        self.assertNotIn(f"{foreign}/sitemap.xml", fetch.calls)
        self.assertNotIn(f"{foreign}/leaked", fetch.calls)

    def test_rejects_non_public_fetch_targets(self) -> None:
        def public_resolver(_host: str) -> list[str]:
            return ["93.184.216.34"]

        def private_resolver(_host: str) -> list[str]:
            return ["10.0.0.8"]

        self.assertTrue(
            is_safe_public_https_url("https://example.com/path", public_resolver)
        )
        for url, resolver in [
            ("http://example.com", public_resolver),
            ("https://user:password@example.com", public_resolver),
            ("https://example.com:8443", public_resolver),
            ("https://127.0.0.1", public_resolver),
            ("https://169.254.169.254/latest/meta-data", public_resolver),
            ("https://metadata.internal/path", private_resolver),
        ]:
            with self.subTest(url=url):
                self.assertFalse(is_safe_public_https_url(url, resolver))

    def test_site_origin_rejects_credentials_query_and_fragment(self) -> None:
        for origin in [
            "https://user@example.com",
            "https://example.com?preview=1",
            "https://example.com#preview",
        ]:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    SiteConfig(name="Unsafe", origin=origin)

    def test_site_contract_rejects_invalid_repository_identifiers(self) -> None:
        for repository in (
            "QuantAlchemy",
            "/qa-website",
            "QuantAlchemy/qa-website/extra",
            "QuantAlchemy/qa-website?tab=readme",
        ):
            with self.subTest(repository=repository):
                with self.assertRaises(ValueError):
                    SiteConfig(
                        name="Unsafe",
                        origin="https://example.com",
                        repository=repository,
                    )

    def test_site_contract_rejects_non_path_expectations(self) -> None:
        for field, value in [
            ("expected_text_paths", "https://foreign.example/llms.txt"),
            ("expected_text_paths", "llms.txt"),
            ("required_canonical_paths", "/progress?preview=1"),
            ("required_canonical_paths", "//foreign.example/progress"),
        ]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    SiteConfig(
                        name="Unsafe",
                        origin="https://example.com",
                        **{field: (value,)},
                    )

    def test_reports_a_non_html_root(self) -> None:
        origin = "https://example.com"
        asset_url = f"{origin}/status.json"
        fetch = FakeFetcher(
            {
                f"{origin}/": FetchReceipt(
                    requested_url=f"{origin}/",
                    status=200,
                    final_url=f"{origin}/",
                    content_type="application/json",
                    body="{}",
                ),
                f"{origin}/robots.txt": FetchReceipt(
                    requested_url=f"{origin}/robots.txt",
                    status=200,
                    final_url=f"{origin}/robots.txt",
                    content_type="text/plain",
                    body=f"Sitemap: {origin}/sitemap.xml\n",
                ),
                f"{origin}/sitemap.xml": FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=(
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"<url><loc>{asset_url}</loc></url>"
                        "</urlset>"
                    ),
                ),
                asset_url: FetchReceipt(
                    requested_url=asset_url,
                    status=200,
                    final_url=asset_url,
                    content_type="application/json",
                    body="{}",
                ),
            }
        )

        audit = audit_site(SiteConfig(name="Example", origin=origin), fetch)

        self.assertIn("ROOT_CONTENT_TYPE", {finding.code for finding in audit.findings})

    def test_reports_malformed_or_empty_sitemap_xml(self) -> None:
        origin = "https://example.com"
        common = {
            f"{origin}/": FetchReceipt(
                requested_url=f"{origin}/",
                status=200,
                final_url=f"{origin}/",
                content_type="text/html",
                body='<link rel="canonical" href="https://example.com/">',
            ),
            f"{origin}/robots.txt": FetchReceipt(
                requested_url=f"{origin}/robots.txt",
                status=200,
                final_url=f"{origin}/robots.txt",
                content_type="text/plain",
                body=f"Sitemap: {origin}/sitemap.xml\n",
            ),
        }

        for body, expected_code in [
            ("<urlset>", "SITEMAP_XML_INVALID"),
            (
                "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'/>",
                "SITEMAP_EMPTY",
            ),
            (
                "<feed><loc>https://example.com/</loc></feed>",
                "SITEMAP_ROOT_INVALID",
            ),
        ]:
            with self.subTest(expected_code=expected_code):
                receipts = dict(common)
                receipts[f"{origin}/sitemap.xml"] = FetchReceipt(
                    requested_url=f"{origin}/sitemap.xml",
                    status=200,
                    final_url=f"{origin}/sitemap.xml",
                    content_type="application/xml",
                    body=body,
                )
                audit = audit_site(
                    SiteConfig(name="Example", origin=origin), FakeFetcher(receipts)
                )
                self.assertIn(
                    expected_code,
                    {finding.code for finding in audit.findings},
                )


if __name__ == "__main__":
    unittest.main()
