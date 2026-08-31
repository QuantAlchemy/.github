#!/usr/bin/env python3
"""Plan or apply canonical GitHub repository homepage settings."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Callable

if __package__:
    from tools.seo_fleet_audit import SiteConfig, parse_repository_homepages
else:
    from seo_fleet_audit import SiteConfig, parse_repository_homepages

MAX_GITHUB_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class HomepageTarget:
    repository: str
    homepage: str


@dataclass(frozen=True)
class HomepageUpdate:
    repository: str
    homepage: str
    observed: str


def load_targets(path: Path) -> list[HomepageTarget]:
    """Load every expected GitHub homepage from the fleet configuration."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["sites"] if isinstance(payload, dict) else payload
    sites = [SiteConfig(**row) for row in rows]
    targets = [
        HomepageTarget(site.repository, site.origin)
        for site in sites
        if site.repository
    ]
    if isinstance(payload, dict):
        classified = parse_repository_homepages(payload)
        targets.extend(
            HomepageTarget(item.repository, item.expected_homepage)
            for item in classified
        )
    return targets


def find_updates(
    targets: list[HomepageTarget],
    homepage_fetcher: Callable[[str], str],
) -> list[HomepageUpdate]:
    """Return only missing or non-canonical homepage settings."""
    updates: list[HomepageUpdate] = []
    for target in targets:
        observed = homepage_fetcher(target.repository).strip()
        if observed not in {target.homepage, f"{target.homepage}/"}:
            updates.append(HomepageUpdate(target.repository, target.homepage, observed))
    return updates


def _run_gh(arguments: list[str]) -> dict[str, object]:
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("gh CLI is unavailable")
    try:
        response = subprocess.run(
            [gh, *arguments],
            capture_output=True,
            check=True,
            text=True,
            timeout=30.0,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"GitHub API failed: {detail}") from exc
    if len(response.stdout.encode("utf-8")) > MAX_GITHUB_RESPONSE_BYTES:
        raise RuntimeError("GitHub response exceeded the size limit")
    payload = json.loads(response.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an invalid repository payload")
    return payload


def fetch_repository_homepage(repository: str) -> str:
    """Read a repository homepage through the authenticated GitHub CLI."""
    payload = _run_gh(["api", f"repos/{repository}"])
    homepage = payload.get("homepage")
    return homepage if isinstance(homepage, str) else ""


def update_repository_homepage(repository: str, homepage: str) -> str:
    """Update a repository homepage and return GitHub's persisted value."""
    payload = _run_gh(
        [
            "api",
            "--method",
            "PATCH",
            f"repos/{repository}",
            "--field",
            f"homepage={homepage}",
        ]
    )
    persisted = payload.get("homepage")
    return persisted if isinstance(persisted, str) else ""


def render_command(update: HomepageUpdate) -> str:
    """Render the exact owner-credential command for one update."""
    return shlex.join(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{update.repository}",
            "--field",
            f"homepage={update.homepage}",
        ]
    )


def apply_updates(
    updates: list[HomepageUpdate],
    homepage_updater: Callable[[str, str], str],
) -> None:
    """Apply every update and reject any response that does not verify it."""
    for update in updates:
        persisted = homepage_updater(update.repository, update.homepage).strip()
        if persisted not in {update.homepage, f"{update.homepage}/"}:
            raise RuntimeError(
                f"verification failed for {update.repository}: "
                f"expected {update.homepage}, observed {persisted or 'empty homepage'}"
            )


def main(
    argv: list[str] | None = None,
    *,
    homepage_fetcher: Callable[[str], str] | None = None,
    homepage_updater: Callable[[str, str], str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/public-sites.json"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write settings with the authenticated gh credential",
    )
    args = parser.parse_args(argv)

    fetcher = homepage_fetcher or fetch_repository_homepage
    updater = homepage_updater or update_repository_homepage
    try:
        updates = find_updates(load_targets(args.config), fetcher)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not updates:
        print("All repository homepages already match the canonical fleet map.")
        return 0

    noun = "update" if len(updates) == 1 else "updates"
    print(f"{len(updates)} repository homepage {noun} required:")
    for update in updates:
        observed = update.observed or "empty homepage"
        target = update.homepage or "empty homepage"
        print(f"- {update.repository}: {observed} -> {target}")
        print(f"  {render_command(update)}")

    if not args.apply:
        print(
            "Re-run with --apply using a GitHub credential with repository admin access."
        )
        return 1

    try:
        apply_updates(updates, updater)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for update in updates:
        print(f"VERIFIED {update.repository}: {update.homepage}")
    print(f"{len(updates)} repository homepage {noun} applied and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
