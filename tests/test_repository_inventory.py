from contextlib import redirect_stdout
from io import StringIO
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tools.seo_fleet_audit as audit


class InventoryTransportTests(unittest.TestCase):
    def fetch_with_cli(self, script):
        self.assertTrue(callable(getattr(audit, "fetch_repository_inventory_page", None)),
                        "missing bounded GitHub inventory transport")
        with TemporaryDirectory() as directory:
            gh = Path(directory) / "gh"
            gh.write_text(f"#!{sys.executable}\nimport sys\n{script}\n", encoding="utf-8")
            gh.chmod(0o700)
            with patch.object(audit.shutil, "which", return_value=str(gh)):
                return audit.fetch_repository_inventory_page("example", 2)

    def test_authenticated_cli_uses_explicit_get_and_page(self):
        result = self.fetch_with_cli(
            "assert sys.argv[1:] == ['api', '--method', 'GET', "
            "'orgs/example/repos?type=all&sort=full_name&direction=asc&per_page=100&page=2']\n"
            "print('[{\"full_name\":\"Example/new\",\"homepage\":null}]')"
        )
        self.assertEqual([{"full_name": "Example/new", "homepage": None}], result)

    def test_cli_errors_and_invalid_json_are_not_empty_inventory(self):
        for script, message in [
            ("print('HTTP 403: denied', file=sys.stderr); sys.exit(1)", "HTTP 403: denied"),
            ("print('not JSON')", "JSON"),
        ]:
            with self.subTest(script=script), self.assertRaisesRegex(ValueError if message == "JSON" else RuntimeError, message):
                self.fetch_with_cli(script)

    def test_cli_output_is_bounded_on_stdout_and_stderr(self):
        for stream in ("stdout", "stderr"):
            with (
                self.subTest(stream=stream),
                patch.object(audit, "MAX_INVENTORY_OUTPUT_BYTES", 128, create=True),
                self.assertRaisesRegex(RuntimeError, "exceeded 128 bytes"),
            ):
                self.fetch_with_cli(f"print('x' * 4096, file=sys.{stream})")

    def test_cli_timeout_is_bounded(self):
        with (
            patch.object(audit, "INVENTORY_TIMEOUT_SECONDS", 0.05, create=True),
            self.assertRaisesRegex(RuntimeError, "timed out"),
        ):
            self.fetch_with_cli("import time; time.sleep(10)")


class RepositoryInventoryTests(unittest.TestCase):
    def run_inventory(self, pages):
        """Exercise the normal CLI with real config and receipt files."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "sites": [],
                "repository_homepages": [{
                    "repository": "Example/known",
                    "classification": "prototype",
                    "expected_homepage": "",
                    "note": "Internal dev/test only.",
                }],
            }), encoding="utf-8")
            original = config.read_bytes()
            output = StringIO()
            with (
                patch.object(audit, "fetch_repository_inventory_page", create=True,
                             side_effect=pages) as fetch_page,
                patch.object(audit, "fetch_repository_homepage", return_value=""),
                patch.object(audit, "audit_site") as audit_site,
                patch.object(audit, "build_opener") as opener,
                redirect_stdout(output),
            ):
                status = audit.main([
                    "--config", str(config),
                    "--json-out", str(root / "receipt.json"),
                    "--markdown-out", str(root / "receipt.md"),
                ])
            self.assertEqual(original, config.read_bytes())
            self.assertEqual(output.getvalue(), (root / "receipt.md").read_text())
            audit_site.assert_not_called()
            opener.assert_not_called()
            return (status, json.loads((root / "receipt.json").read_text()),
                    output.getvalue(), fetch_page.call_args_list)

    def test_normal_cli_reports_unknown_homepage_without_fetch_or_enrollment(self):
        observed = " http://127.0.0.1/private?preview=1#Exact "
        status, payload, markdown, calls = self.run_inventory([[
            {"full_name": "EXAMPLE/KNOWN", "homepage": None},
            {"full_name": "Example/new-dev", "homepage": observed},
            {"full_name": "Example/empty", "homepage": ""},
        ]])
        self.assertEqual(1, status)
        self.assertFalse(payload["healthy"])
        self.assertEqual(1, len(calls))
        inventory = payload["repositoryInventory"][0]
        self.assertEqual("example", inventory["organization"])
        self.assertTrue(inventory["complete"])
        self.assertFalse(inventory["healthy"])
        self.assertEqual(3, inventory["checkedRepositories"])
        self.assertEqual(1, inventory["nonemptyHomepages"])
        self.assertEqual(1, inventory["classifiedRepositories"])
        self.assertEqual(1, inventory["unclassifiedHomepages"])
        self.assertEqual([{
            "code": "REPOSITORY_HOMEPAGE_UNCLASSIFIED",
            "url": "https://github.com/Example/new-dev",
            "expected": "an explicit sites or repository_homepages policy",
            "observed": observed,
        }], inventory["findings"])
        self.assertIn("REPOSITORY_HOMEPAGE_UNCLASSIFIED", markdown)
        self.assertIn(observed, markdown)
        self.assertIn("3 visible repositories", markdown)
        self.assertIn("1 nonempty homepages", markdown)
        self.assertIn("1 classified repositories", markdown)
        self.assertIn("1 unclassified homepages", markdown)
        self.assertIn("1 defects", markdown)

    def test_inventory_pages_dedupe_casefolded_repositories_deterministically(self):
        first = [
            {"full_name": "Example/z-dev", "homepage": "https://z.example"},
            {"full_name": "Example/known", "homepage": ""},
        ]
        second = [
            {"full_name": "EXAMPLE/Z-DEV", "homepage": "https://z.example"},
            {"full_name": "Example/a-dev", "homepage": "https://a.example"},
        ]
        results = []
        for pages in ([first, second, []], [second, first, []]):
            with patch.object(audit, "INVENTORY_PAGE_SIZE", 2, create=True):
                status, payload, _, calls = self.run_inventory(pages)
            self.assertEqual(1, status)
            self.assertEqual([("example", 1), ("example", 2), ("example", 3)],
                             [call.args for call in calls])
            inventory = payload["repositoryInventory"][0]
            self.assertEqual(3, inventory["checkedRepositories"])
            self.assertEqual(3, inventory["pageCount"])
            self.assertEqual(2, inventory["unclassifiedHomepages"])
            self.assertEqual(2, inventory["nonemptyHomepages"])
            results.append(inventory)
        self.assertEqual(results[0], results[1])
        self.assertEqual([
            "https://github.com/Example/a-dev", "https://github.com/EXAMPLE/Z-DEV",
        ], [item["url"] for item in results[0]["findings"]])

    def test_healthy_inventory_is_included_in_receipts(self):
        status, payload, markdown, _ = self.run_inventory([[
            {"full_name": "example/KNOWN", "homepage": ""},
            {"full_name": "Example/empty", "homepage": None},
        ]])
        self.assertEqual(0, status)
        self.assertTrue(payload["healthy"])
        self.assertIn("repositoryInventory", payload)
        self.assertTrue(payload["repositoryInventory"][0]["healthy"])
        self.assertIn("Inventory: complete", markdown)

    def test_inventory_failures_emit_receipts_and_operational_exit(self):
        cases = [
            RuntimeError("HTTP 403: Resource not accessible by integration"),
            {"message": "unexpected object"},
            [None],
            [{"full_name": "Example/missing"}],
            [{"homepage": "https://dev.example"}],
            [{"full_name": "Example/new", "homepage": 42}],
            [{"full_name": "Example/new", "homepage": False}],
            [{"full_name": "Other/new", "homepage": "https://dev.example"}],
            [{"full_name": "Example/new?x=1", "homepage": ""}],
            [{"full_name": 42, "homepage": ""}],
            [{"full_name": "Example/..", "homepage": ""}],
            [{"full_name": "Example/new", "homepage": "a"},
             {"full_name": "example/NEW", "homepage": "b"}],
        ]
        for page in cases:
            with self.subTest(page=page):
                status, payload, markdown, _ = self.run_inventory([page])
                self.assertEqual(2, status)
                self.assertFalse(payload["healthy"])
                inventory = payload["repositoryInventory"][0]
                self.assertFalse(inventory["complete"])
                self.assertFalse(inventory["healthy"])
                self.assertEqual("REPOSITORY_INVENTORY_LOOKUP_FAILED",
                                 inventory["findings"][-1]["code"])
                self.assertEqual("https://api.github.com/orgs/example/repos",
                                 inventory["findings"][-1]["url"])
                self.assertIn("Inventory: incomplete", markdown)
                self.assertIn("REPOSITORY_INVENTORY_LOOKUP_FAILED", markdown)

    def test_later_page_failure_keeps_prior_findings_but_exits_two(self):
        with patch.object(audit, "INVENTORY_PAGE_SIZE", 1, create=True):
            status, payload, _, _ = self.run_inventory([
                [{"full_name": "Example/new", "homepage": "https://dev.example"}],
                RuntimeError("rate limited"),
            ])
        self.assertEqual(2, status)
        inventory = payload["repositoryInventory"][0]
        self.assertEqual(1, inventory["checkedRepositories"])
        self.assertEqual(1, inventory["pageCount"])
        self.assertEqual({"REPOSITORY_HOMEPAGE_UNCLASSIFIED",
                          "REPOSITORY_INVENTORY_LOOKUP_FAILED"},
                         {item["code"] for item in inventory["findings"]})

    def test_page_limit_fails_closed(self):
        with (
            patch.object(audit, "INVENTORY_PAGE_SIZE", 1, create=True),
            patch.object(audit, "MAX_INVENTORY_PAGES", 1, create=True),
        ):
            status, payload, _, calls = self.run_inventory([
                [{"full_name": "Example/known", "homepage": ""}],
            ])
        self.assertEqual(2, status)
        self.assertEqual(1, len(calls))
        self.assertIn("page limit", payload["repositoryInventory"][0]["findings"][-1]["observed"])

    def test_invalid_unicode_homepage_fails_inventory_before_receipt_writing(self):
        policy = audit.RepositoryHomepageConfig("Example/known", "prototype", "", "Dev/test only.")
        inventories = audit.audit_repository_inventory([], [policy], lambda *_: [
            {"full_name": "Example/new", "homepage": "https://dev.example/\ud800"},
        ])
        self.assertFalse(inventories[0].complete)
        self.assertEqual("REPOSITORY_INVENTORY_LOOKUP_FAILED", inventories[0].findings[-1].code)

    def test_inventory_observations_cannot_break_out_of_markdown_code(self):
        observed = "https://dev.example/`\n```\n<img src=x>"
        status, payload, markdown, _ = self.run_inventory([[
            {"full_name": "Example/new", "homepage": observed},
        ]])
        self.assertEqual(1, status)
        self.assertEqual(observed, payload["repositoryInventory"][0]["findings"][0]["observed"])
        self.assertIn(f"\n````text\n{observed}\n````\n", markdown)

    def test_owners_from_both_config_lists_are_deduplicated_and_sorted(self):
        sites = [
            audit.SiteConfig("Zulu", "https://z.example", "Zulu/public"),
            audit.SiteConfig("Alpha", "https://a.example", "ALPHA/public"),
        ]
        policies = [audit.RepositoryHomepageConfig("zULU/dev", "prototype", "", "Dev/test only.")]
        pages = {
            "alpha": [{"full_name": "Alpha/public", "homepage": "https://a.example"}],
            "zulu": [{"full_name": "ZULU/PUBLIC", "homepage": "https://z.example"},
                     {"full_name": "Zulu/dev", "homepage": ""}],
        }
        with patch.object(audit, "fetch_repository_inventory_page",
                          side_effect=lambda org, page: pages[org]) as fetch:
            inventories = audit.audit_repository_inventory(sites, policies, fetch)
        self.assertEqual([("alpha", 1), ("zulu", 1)], [call.args for call in fetch.call_args_list])
        self.assertTrue(all(item.healthy for item in inventories))
        self.assertEqual([1, 2], [item.classified_repositories for item in inventories])

    def test_legacy_site_list_derives_inventory_without_extra_config_keys(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / "sites.json"
            config.write_text(json.dumps([{
                "name": "Example", "origin": "https://example.com", "repository": "Example/public",
            }]), encoding="utf-8")
            with (
                patch.object(audit, "fetch_repository_inventory_page", return_value=[
                    {"full_name": "example/PUBLIC", "homepage": "https://example.com"},
                ]) as fetch,
                patch.object(audit, "fetch_repository_homepage", return_value="https://example.com"),
                patch.object(audit, "audit_site", side_effect=lambda site, **_: audit.SiteAudit(site)),
                redirect_stdout(StringIO()),
            ):
                status = audit.main(["--config", str(config)])
        self.assertEqual(0, status)
        fetch.assert_called_once_with("example", 1)

    def test_real_cli_process_writes_receipts_for_all_inventory_exit_statuses(self):
        for inventory, expected_status in [
            ([], 0),
            ([{"full_name": "Example/new", "homepage": "https://dev.example"}], 1),
            ({"message": "malformed successful API response"}, 2),
        ]:
            with self.subTest(status=expected_status), TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "sites.json"
                config.write_text(json.dumps({"sites": [], "repository_homepages": [{
                    "repository": "Example/dev", "classification": "prototype",
                    "expected_homepage": "", "note": "Dev/test only.",
                }]}), encoding="utf-8")
                gh = root / "gh"
                gh.write_text(
                    f"#!{sys.executable}\nimport sys\n"
                    "if sys.argv[1:] == ['api', 'repos/Example/dev']:\n"
                    "    print('{\"homepage\":\"\"}')\n"
                    "else:\n"
                    "    assert sys.argv[1:4] == ['api', '--method', 'GET']\n"
                    "    assert sys.argv[4].startswith('orgs/example/repos?')\n"
                    f"    print({json.dumps(inventory)!r})\n", encoding="utf-8",
                )
                gh.chmod(0o700)
                result = subprocess.run([
                    sys.executable, "-B", "tools/seo_fleet_audit.py", "--config", str(config),
                    "--json-out", str(root / "receipt.json"),
                    "--markdown-out", str(root / "receipt.md"),
                ], capture_output=True, text=True, timeout=5,
                    env={**os.environ, "PATH": f"{root}{os.pathsep}{os.environ.get('PATH', '')}"})
                self.assertEqual(expected_status, result.returncode, result.stderr)
                self.assertEqual("", result.stderr)
                self.assertEqual(result.stdout, (root / "receipt.md").read_text())
                payload = json.loads((root / "receipt.json").read_text())
                self.assertEqual(expected_status == 0, payload["healthy"])
                self.assertEqual(expected_status != 2, payload["repositoryInventory"][0]["complete"])


if __name__ == "__main__":
    unittest.main()
