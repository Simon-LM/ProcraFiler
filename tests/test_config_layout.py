import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
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
