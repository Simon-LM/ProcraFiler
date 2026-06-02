# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _prune_empty_inbox_dirs, process_all_inbox_files


class TestPruneEmptyInboxDirs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "Inbox"
        self.inbox.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_removes_nested_empty_dirs_bottom_up(self) -> None:
        (self.inbox / "a" / "b" / "c").mkdir(parents=True)
        removed = _prune_empty_inbox_dirs(self.inbox)
        self.assertEqual(removed, 3)
        self.assertFalse((self.inbox / "a").exists())
        self.assertTrue(self.inbox.exists())  # root kept

    def test_never_removes_the_inbox_root(self) -> None:
        removed = _prune_empty_inbox_dirs(self.inbox)  # root is empty
        self.assertEqual(removed, 0)
        self.assertTrue(self.inbox.exists())

    def test_keeps_dir_that_still_holds_a_file(self) -> None:
        keep = self.inbox / "keep"
        keep.mkdir()
        (keep / "doc.txt").write_text("x", encoding="utf-8")
        (self.inbox / "empty").mkdir()
        _prune_empty_inbox_dirs(self.inbox)
        self.assertTrue(keep.exists())
        self.assertFalse((self.inbox / "empty").exists())

    def test_partially_empty_tree_keeps_branch_with_file(self) -> None:
        (self.inbox / "x" / "empty").mkdir(parents=True)
        (self.inbox / "x" / "full").mkdir(parents=True)
        (self.inbox / "x" / "full" / "f.txt").write_text("x", encoding="utf-8")
        _prune_empty_inbox_dirs(self.inbox)
        self.assertFalse((self.inbox / "x" / "empty").exists())
        self.assertTrue((self.inbox / "x" / "full" / "f.txt").exists())
        self.assertTrue((self.inbox / "x").exists())  # kept: still has "full"

    def test_symlinked_dir_pointing_outside_is_not_followed_or_removed(self) -> None:
        external = Path(self.tmp.name) / "external"
        external.mkdir()
        (external / "precious.txt").write_text("keep me", encoding="utf-8")
        link = self.inbox / "link"
        link.symlink_to(external, target_is_directory=True)

        _prune_empty_inbox_dirs(self.inbox)

        # The symlink and, crucially, the external target + its file are untouched.
        self.assertTrue(link.is_symlink())
        self.assertTrue((external / "precious.txt").exists())


class TestPrunePipelineIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_process_all_removes_emptied_subfolders_but_keeps_inbox(self) -> None:
        (self.paths.inbox_dir / "CV").mkdir()
        (self.paths.inbox_dir / "CV" / "cv.txt").write_bytes(b"mon cv")
        (self.paths.inbox_dir / "Photos" / "sub").mkdir(parents=True)
        (self.paths.inbox_dir / "Photos" / "sub" / "note.txt").write_bytes(b"une note")

        process_all_inbox_files(self.paths, now_utc=self.now)

        # The processed files left their subfolders empty → removed.
        self.assertFalse((self.paths.inbox_dir / "CV").exists())
        self.assertFalse((self.paths.inbox_dir / "Photos").exists())
        # The Inbox drop point itself remains.
        self.assertTrue(self.paths.inbox_dir.exists())
        # The files are in the library, not lost.
        filed = [p for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertEqual(len(filed), 2)


if __name__ == "__main__":
    unittest.main()
