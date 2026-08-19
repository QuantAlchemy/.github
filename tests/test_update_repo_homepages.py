from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import tools.update_repo_homepages as update_repo_homepages


class UpdateRepoHomepagesTests(unittest.TestCase):
    def test_script_entrypoint_runs_from_the_repository_root(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/update_repo_homepages.py", "--help"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Plan or apply canonical GitHub", result.stdout)

    def test_load_targets_uses_repository_and_origin_from_fleet_config(self) -> None:
        with TemporaryDirectory() as directory:
            config = Path(directory) / "sites.json"
            config.write_text(
                json.dumps(
                    {
                        "sites": [
                            {
                                "name": "Example",
                                "origin": "https://www.example.com",
                                "repository": "Example/site",
                            },
                            {
                                "name": "No repository",
                                "origin": "https://docs.example.com",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            targets = update_repo_homepages.load_targets(config)

        self.assertEqual(
            [
                update_repo_homepages.HomepageTarget(
                    repository="Example/site",
                    homepage="https://www.example.com",
                )
            ],
            targets,
        )

    def test_find_updates_reports_missing_and_mismatched_homepages_only(self) -> None:
        targets = [
            update_repo_homepages.HomepageTarget(
                "Example/healthy", "https://healthy.example.com"
            ),
            update_repo_homepages.HomepageTarget(
                "Example/slash", "https://slash.example.com"
            ),
            update_repo_homepages.HomepageTarget(
                "Example/missing", "https://missing.example.com"
            ),
            update_repo_homepages.HomepageTarget(
                "Example/preview", "https://preview.example.com"
            ),
        ]
        current = {
            "Example/healthy": "https://healthy.example.com",
            "Example/slash": "https://slash.example.com/",
            "Example/missing": "",
            "Example/preview": "https://preview-host.example.net",
        }

        updates = update_repo_homepages.find_updates(targets, current.__getitem__)

        self.assertEqual(
            [
                update_repo_homepages.HomepageUpdate(
                    "Example/missing", "https://missing.example.com", ""
                ),
                update_repo_homepages.HomepageUpdate(
                    "Example/preview",
                    "https://preview.example.com",
                    "https://preview-host.example.net",
                ),
            ],
            updates,
        )

    def test_github_api_failure_preserves_actionable_error_text(self) -> None:
        failure = subprocess.CalledProcessError(
            1,
            ["gh", "api"],
            stderr="HTTP 403: Resource not accessible by integration",
        )

        with (
            patch.object(
                update_repo_homepages.shutil,
                "which",
                return_value="/authenticated/gh",
            ),
            patch.object(
                update_repo_homepages.subprocess,
                "run",
                side_effect=failure,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "GitHub API failed.*Resource not accessible by integration",
            ),
        ):
            update_repo_homepages.fetch_repository_homepage("Example/site")

    def test_update_repository_homepage_uses_patch_and_returns_persisted_value(
        self,
    ) -> None:
        response = Mock(stdout='{"homepage":"https://www.example.com"}\n')

        with (
            patch.object(
                update_repo_homepages.shutil,
                "which",
                return_value="/authenticated/gh",
            ),
            patch.object(
                update_repo_homepages.subprocess,
                "run",
                return_value=response,
            ) as run,
        ):
            persisted = update_repo_homepages.update_repository_homepage(
                "Example/site", "https://www.example.com"
            )

        self.assertEqual("https://www.example.com", persisted)
        run.assert_called_once_with(
            [
                "/authenticated/gh",
                "api",
                "--method",
                "PATCH",
                "repos/Example/site",
                "--field",
                "homepage=https://www.example.com",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=30.0,
        )

    def test_apply_updates_verifies_each_github_response(self) -> None:
        updates = [
            update_repo_homepages.HomepageUpdate(
                "Example/site",
                "https://www.example.com",
                "https://preview.example.net",
            )
        ]
        calls: list[tuple[str, str]] = []

        def update(repository: str, homepage: str) -> str:
            calls.append((repository, homepage))
            return homepage

        update_repo_homepages.apply_updates(updates, update)

        self.assertEqual(
            [("Example/site", "https://www.example.com")],
            calls,
        )

    def test_apply_updates_rejects_an_unverified_write(self) -> None:
        updates = [
            update_repo_homepages.HomepageUpdate(
                "Example/site",
                "https://www.example.com",
                "https://preview.example.net",
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            update_repo_homepages.apply_updates(
                updates, lambda _repository, _homepage: "https://wrong.example.com"
            )

    def test_dry_run_prints_scriptable_commands_without_writing(self) -> None:
        with TemporaryDirectory() as directory:
            config = Path(directory) / "sites.json"
            config.write_text(
                json.dumps(
                    {
                        "sites": [
                            {
                                "name": "Example",
                                "origin": "https://www.example.com",
                                "repository": "Example/site",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            writes: list[tuple[str, str]] = []
            output = StringIO()

            with redirect_stdout(output):
                status = update_repo_homepages.main(
                    ["--config", str(config)],
                    homepage_fetcher=lambda _repository: "https://preview.example.net",
                    homepage_updater=lambda repository, homepage: (
                        writes.append((repository, homepage)) or homepage
                    ),
                )

        self.assertEqual(1, status)
        self.assertEqual([], writes)
        self.assertIn("1 repository homepage update required", output.getvalue())
        self.assertIn(
            "gh api --method PATCH repos/Example/site --field "
            "homepage=https://www.example.com",
            output.getvalue(),
        )
        self.assertIn("Re-run with --apply", output.getvalue())

    def test_apply_mode_reports_permission_failure_without_a_traceback(self) -> None:
        with TemporaryDirectory() as directory:
            config = Path(directory) / "sites.json"
            config.write_text(
                json.dumps(
                    {
                        "sites": [
                            {
                                "name": "Example",
                                "origin": "https://www.example.com",
                                "repository": "Example/site",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()

            def deny_update(_repository: str, _homepage: str) -> str:
                raise RuntimeError("GitHub API failed: HTTP 403")

            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = update_repo_homepages.main(
                    ["--config", str(config), "--apply"],
                    homepage_fetcher=lambda _repository: "",
                    homepage_updater=deny_update,
                )

        self.assertEqual(2, status)
        self.assertIn("ERROR: GitHub API failed: HTTP 403", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_apply_mode_updates_and_reports_verified_receipt(self) -> None:
        with TemporaryDirectory() as directory:
            config = Path(directory) / "sites.json"
            config.write_text(
                json.dumps(
                    {
                        "sites": [
                            {
                                "name": "Example",
                                "origin": "https://www.example.com",
                                "repository": "Example/site",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output = StringIO()

            with redirect_stdout(output):
                status = update_repo_homepages.main(
                    ["--config", str(config), "--apply"],
                    homepage_fetcher=lambda _repository: "",
                    homepage_updater=lambda _repository, homepage: homepage,
                )

        self.assertEqual(0, status)
        self.assertIn("VERIFIED Example/site", output.getvalue())
        self.assertIn("1 repository homepage update applied", output.getvalue())


if __name__ == "__main__":
    unittest.main()
