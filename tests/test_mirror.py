# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.mirror import purge_mirror_trash, sync_library_file_to_mirror  # type: ignore[reportMissingImports]


class TestMirrorRetention(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sync_quarantines_previous_mirror_version(self) -> None:
        source = self.paths.library_root / "Unsorted" / "resume.txt"
        source.parent.mkdir(parents=True, exist_ok=True)

        source.write_text("v1", encoding="utf-8")
        first = sync_library_file_to_mirror(self.paths, source, now_utc=datetime(2026, 4, 2, 11, 0, 0, tzinfo=timezone.utc))
        self.assertTrue(first.success)

        source.write_text("v2", encoding="utf-8")
        second = sync_library_file_to_mirror(self.paths, source, now_utc=datetime(2026, 4, 2, 11, 5, 0, tzinfo=timezone.utc))
        self.assertTrue(second.success)

        mirror_target = self.paths.mirror_root / "Unsorted" / "resume.txt"
        self.assertEqual(mirror_target.read_text(encoding="utf-8"), "v2")

        trash_files = [p for p in self.paths.mirror_trash_dir.rglob("*") if p.is_file()]
        self.assertEqual(len(trash_files), 1)
        self.assertEqual(trash_files[0].read_text(encoding="utf-8"), "v1")

    def test_purge_mirror_trash_applies_ttl(self) -> None:
        old_file = self.paths.mirror_trash_dir / "old.txt"
        new_file = self.paths.mirror_trash_dir / "new.txt"
        old_file.parent.mkdir(parents=True, exist_ok=True)

        old_file.write_text("old", encoding="utf-8")
        new_file.write_text("new", encoding="utf-8")

        now = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
        old_ts = (now - timedelta(days=40)).timestamp()
        new_ts = (now - timedelta(days=2)).timestamp()
        os.utime(old_file, (old_ts, old_ts))
        os.utime(new_file, (new_ts, new_ts))

        removed = purge_mirror_trash(self.paths, retention_days=30, now_utc=now)
        self.assertEqual(removed, 1)
        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())


if __name__ == "__main__":
    unittest.main()
