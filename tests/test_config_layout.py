import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.config import default_runtime_paths, ensure_runtime_layout


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

            self.assertTrue((paths.library_root / "Personnel" / "Documents").exists())
            self.assertTrue((paths.library_root / "Professionnel" / "Documents").exists())
            self.assertTrue((paths.library_root / "Administratif").exists())
            self.assertTrue((paths.library_root / "Banque").exists())
            self.assertTrue((paths.library_root / "Telephonie").exists())
            self.assertTrue((paths.library_root / "Revue_Manuelle").exists())


if __name__ == "__main__":
    unittest.main()
