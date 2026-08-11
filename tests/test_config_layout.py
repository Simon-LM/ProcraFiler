import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import json

from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    format_paths_json,
    paths_report,
)
from procrafiler.dev_guard import ROOT_ENV_VARS


class TestProductionDefaults(unittest.TestCase):
    """Where a real user's documents live, pinned.

    Nothing asserted these until a mutation run went unnoticed: renaming the
    production library default broke no test, while it would strand every existing
    installation — the app would look for the library somewhere it has never been,
    find nothing, and offer to create a fresh one beside it.

    `force_home_defaults` is how a source checkout asks the production question;
    without it these are the checkout's own sandbox, which is the point of that
    flag and is covered in `test_dev_guard`.
    """

    def _production_paths(self, home: Path):
        with patch.object(Path, "home", return_value=home):
            with patch.dict(os.environ, {}, clear=False):
                for name in ROOT_ENV_VARS:
                    os.environ.pop(name, None)
                return default_runtime_paths(force_home_defaults=True)

    def test_the_documented_layout_is_what_the_code_computes(self) -> None:
        """These five lines are published in docs/dev-prod-isolation.md as what an
        unconfigured run targets. If the code and the document disagree, one of them
        is lying to a user about where their documents are."""
        home = Path("/home/someone")
        paths = self._production_paths(home)
        self.assertEqual(paths.workspace_root, home / "Downloads" / "ProcraFiler_Inbox")
        self.assertEqual(paths.queue_dir, home / "Downloads" / "ProcraFiler_Inbox" / "Queue")
        self.assertEqual(paths.library_root, home / "ProcraFiler_Library")
        self.assertEqual(paths.mirror_root, home / "ProcraFiler_Library_Mirror")
        self.assertEqual(paths.state_root, home / ".local" / "share" / "procrafiler")
        self.assertEqual(paths.settings_file.parent, home / ".config" / "procrafiler")

    def test_an_environment_variable_still_wins(self) -> None:
        """Anti-vacuity: pinning the defaults must not freeze the overrides too. A
        user who moved their library must still be followed there."""
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = Path(tmp) / "on-another-disk"
            with patch.dict(os.environ, {"PROCRAFILER_LIBRARY_DIR": str(elsewhere)}, clear=False):
                paths = default_runtime_paths(force_home_defaults=True)
            self.assertEqual(paths.library_root, elsewhere)


class TestPathsReport(unittest.TestCase):
    """What the install scripts read instead of restating this module in bash.

    They had no other option and they drifted: the purge list came to name a
    `search_index.db` a given layout never had, while missing the runtime lock, the
    state directory itself and four stale subdirectories left by older versions.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for key, value in {
            "PROCRAFILER_WORKSPACE_DIR": self.root / "inbox",
            "PROCRAFILER_LIBRARY_DIR": self.root / "lib",
            "PROCRAFILER_LIBRARY_MIRROR_DIR": self.root / "mirror",
            "PROCRAFILER_HOME": self.root / "state",
            "PROCRAFILER_CONFIG_HOME": self.root / "config",
        }.items():
            os.environ[key] = str(value)
        self.report = paths_report(default_runtime_paths())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_nothing_preserved_may_also_be_purged(self) -> None:
        """The single most important assertion here. An uninstaller that can reach
        the documents is not an uninstaller."""
        purge = (set(self.report["purge_files"]) | set(self.report["purge_dirs"])
                 | set(self.report["personal_files"]))
        for kept in self.report["preserve"]:
            with self.subTest(kept=kept):
                self.assertNotIn(kept, purge)

    def test_the_state_ROOT_is_purged_whole(self) -> None:
        """Not merely its files: the lock and the stale subdirectories of older
        versions live there, and leaving them is what made "purged" untrue."""
        self.assertEqual(self.report["purge_dirs"], [str(self.root / "state")])

    def test_the_config_root_is_NOT_purged_whole(self) -> None:
        """The user's own context file lives beside the app's settings. It IS purged
        — leaving personal notes on a machine somebody just wiped the app from is a
        leak — but only after they have been offered a copy, and an `rm -rf` on the
        directory would take it without ever asking."""
        self.assertNotIn(str(self.root / "config"), self.report["purge_dirs"])

    def test_the_context_file_is_purged_but_only_through_the_offer(self) -> None:
        """Listed apart from `purge_files` on purpose: everything in that list is
        deleted without a word, and this one may not be."""
        for name in ("context.txt", "context.md"):
            path = str(self.root / "config" / name)
            with self.subTest(name=name):
                self.assertIn(path, self.report["personal_files"])
                self.assertNotIn(path, self.report["purge_files"])
                self.assertNotIn(path, self.report["preserve"], "it must not be kept either")

    def test_the_config_files_the_app_owns_are_purgeable(self) -> None:
        """Anti-vacuity: sparing the directory must not spare its contents."""
        for name in ("settings.json", "policy.toml"):
            with self.subTest(name=name):
                self.assertIn(str(self.root / "config" / name), self.report["purge_files"])

    def test_every_state_file_this_module_knows_about_is_listed(self) -> None:
        """The drift guard. A path added to `RuntimePaths` under the state or config
        root and forgotten here would survive a purge for ever."""
        paths = default_runtime_paths()
        listed = set(self.report["purge_files"])
        roots = (str(paths.state_root), str(paths.settings_file.parent))
        for name, value in vars(paths).items():
            if name.endswith("_file") and str(value).startswith(roots):
                with self.subTest(field=name):
                    self.assertIn(str(value), listed, f"{name} would survive a purge")

    def test_it_is_valid_json_and_carries_a_schema(self) -> None:
        decoded = json.loads(format_paths_json(default_runtime_paths()))
        self.assertEqual(decoded["schema"], 2)
        self.assertEqual(decoded["paths"]["state_root"], str(self.root / "state"))


class TestConfigLayout(unittest.TestCase):
    def test_default_layout_matches_mvp_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(Path(tmp) / "ProcraFiler_Inbox")
            os.environ["PROCRAFILER_LIBRARY_DIR"] = str(Path(tmp) / "ProcraFiler_Library")
            os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(Path(tmp) / "ProcraFiler_Library_Mirror")
            os.environ["PROCRAFILER_HOME"] = str(Path(tmp) / ".state")
            os.environ["PROCRAFILER_CONFIG_HOME"] = str(Path(tmp) / ".config")

            paths = default_runtime_paths()
            ensure_runtime_layout(paths)

            self.assertTrue(paths.inbox_dir.name == "Inbox")
            self.assertTrue(paths.queue_dir.name == "Queue")
            self.assertTrue(paths.inbox_trash_manual_dir.name == "Inbox_Trash_Manual")
            self.assertTrue(paths.library_root.name == "ProcraFiler_Library")
            self.assertTrue(paths.library_trash_manual_dir.name == "ProcraFiler_Library_Trash_Manual")
            self.assertTrue(paths.mirror_root.name == "ProcraFiler_Library_Mirror")
            self.assertTrue(paths.mirror_trash_dir.name == "Mirror_Trash")

            self.assertTrue(paths.actions_log_file.exists())
            self.assertTrue(paths.catalog_db_file.exists())
            self.assertTrue(paths.catalog_snapshot_file.exists())

            self.assertTrue((paths.library_root / "Personal" / "Administrative").exists())
            self.assertTrue((paths.library_root / "Personal" / "Administrative" / "Taxes").exists())
            self.assertTrue((paths.library_root / "Personal" / "Administrative" / "Utilities").exists())
            self.assertTrue((paths.library_root / "Personal" / "Hobbies").exists())
            self.assertTrue((paths.library_root / "Work" / "Employment" / "Payslips").exists())
            self.assertTrue((paths.library_root / "Work" / "Business" / "Clients").exists())
            self.assertTrue((paths.library_root / "Manual_Review").exists())


if __name__ == "__main__":
    unittest.main()
