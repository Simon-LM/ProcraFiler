# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files, process_next_inbox_file


class TestInboxRecursion(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(self.root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(self.root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(self.root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(self.root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(self.root / ".config")
        # Deterministic: no AI chain configured -> everything routes to manual
        # review, no network. (Clears any chain leaked from another test's .env.)
        for key in [k for k in os.environ if k.startswith("PROCRAFILER_AI_")]:
            os.environ.pop(key, None)
        os.environ.pop("MISTRAL_API_KEY", None)
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _library_files(self) -> list[Path]:
        return [p for p in self.paths.library_root.rglob("*") if p.is_file()]

    def test_file_in_subfolder_is_processed(self) -> None:
        sub = self.paths.inbox_dir / "a" / "b"
        sub.mkdir(parents=True)
        (sub / "deep.txt").write_bytes(b"contenu en profondeur")

        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")
        self.assertEqual(len(self._library_files()), 1)

    def test_process_all_handles_nested_files(self) -> None:
        (self.paths.inbox_dir / "top.txt").write_bytes(b"racine")
        (self.paths.inbox_dir / "x").mkdir()
        (self.paths.inbox_dir / "x" / "mid.txt").write_bytes(b"niveau 1")
        (self.paths.inbox_dir / "x" / "y" / "z").mkdir(parents=True)
        (self.paths.inbox_dir / "x" / "y" / "z" / "low.txt").write_bytes(b"niveau 3")

        summary = process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(summary["processed"], 3)
        self.assertEqual(len(self._library_files()), 3)

    def test_symlinked_file_pointing_outside_inbox_is_ignored(self) -> None:
        # A file OUTSIDE the inbox, linked INTO it, must never be read.
        outside_dir = self.root / "outside_area"
        outside_dir.mkdir()
        external = outside_dir / "secret.txt"
        external.write_bytes(b"NE DOIT PAS ETRE LU")

        (self.paths.inbox_dir / "real.txt").write_bytes(b"document legitime")
        (self.paths.inbox_dir / "escape.txt").symlink_to(external)

        summary = process_all_inbox_files(self.paths, now_utc=self.now)

        # Only the legitimate file was processed.
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(len(self._library_files()), 1)
        # The external file is untouched and never entered the library.
        self.assertTrue(external.exists())
        self.assertEqual(external.read_bytes(), b"NE DOIT PAS ETRE LU")
        self.assertFalse(any(p.read_bytes() == b"NE DOIT PAS ETRE LU" for p in self._library_files()))

    def test_symlinked_directory_escaping_inbox_is_not_descended(self) -> None:
        outside_dir = self.root / "outside_tree"
        outside_dir.mkdir()
        (outside_dir / "hidden.txt").write_bytes(b"HORS INBOX")

        (self.paths.inbox_dir / "ok.txt").write_bytes(b"dans l'inbox")
        (self.paths.inbox_dir / "linked_dir").symlink_to(outside_dir, target_is_directory=True)

        summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["processed"], 1)
        self.assertTrue((outside_dir / "hidden.txt").exists())
        self.assertFalse(any(p.read_bytes() == b"HORS INBOX" for p in self._library_files()))


if __name__ == "__main__":
    unittest.main()
