from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UNINSTALL = _REPO_ROOT / "scripts" / "uninstall.sh"


@unittest.skipUnless(shutil.which("bash") and _UNINSTALL.is_file(), "bash / uninstall.sh unavailable")
class TestUninstallScript(unittest.TestCase):
    """The cardinal install/uninstall guarantee: uninstall NEVER deletes the user's library."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        for d in (".local/share/procrafiler/app", ".local/bin", ".config/procrafiler",
                  "ProcraFiler_Library/Personal"):
            (self.home / d).mkdir(parents=True)
        for f in (".local/bin/procrafiler", ".config/procrafiler/procrafiler.env",
                  ".config/procrafiler/settings.json", ".config/procrafiler/policy.toml",
                  ".config/procrafiler/context.md", ".local/share/procrafiler/catalog.db",
                  ".local/share/procrafiler/actions_log.jsonl", ".local/share/procrafiler/search_index.db",
                  ".local/share/procrafiler/catalog_snapshot.json"):
            (self.home / f).write_text("x", encoding="utf-8")
        self.doc = self.home / "ProcraFiler_Library/Personal/mydoc.txt"
        self.doc.write_text("my important document", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)
        subprocess.run(
            ["bash", str(_UNINSTALL), "--mode", "user", *args],
            env=env, capture_output=True, text=True, check=True,
        )

    def _exists(self, rel: str) -> bool:
        return (self.home / rel).exists()

    def test_default_removes_app_keeps_all_user_data(self) -> None:
        self._run()
        self.assertFalse(self._exists(".local/share/procrafiler/app"))   # app removed
        self.assertFalse(self._exists(".local/bin/procrafiler"))          # launcher removed
        self.assertTrue(self.doc.exists())                                # LIBRARY kept
        self.assertTrue(self._exists(".config/procrafiler/procrafiler.env"))  # config kept
        self.assertTrue(self._exists(".local/share/procrafiler/catalog.db"))  # state kept

    def test_purge_removes_config_and_state_but_never_the_library(self) -> None:
        self._run("--purge", "--yes")
        self.assertFalse(self._exists(".config/procrafiler/procrafiler.env"))   # env purged
        self.assertFalse(self._exists(".config/procrafiler/settings.json"))     # settings purged
        self.assertFalse(self._exists(".local/share/procrafiler/catalog.db"))   # state purged
        self.assertFalse(self._exists(".local/share/procrafiler/search_index.db"))
        self.assertTrue(self.doc.exists())                                      # LIBRARY SACRED
        self.assertTrue(self._exists(".config/procrafiler/context.md"))         # user-authored kept


if __name__ == "__main__":
    unittest.main()
