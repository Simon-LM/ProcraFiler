# pyright: reportUnknownVariableType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    set_feature_flag,
)
from procrafiler.pipeline import process_next_inbox_file


class TestFeatureFlagsApplied(unittest.TestCase):
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

    def test_actions_log_off_suppresses_log_writes(self) -> None:
        set_feature_flag(self.paths, "actions_log", False)
        (self.paths.inbox_dir / "doc.pdf").write_bytes(b"content")

        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")

        lines = [
            line
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(lines, [], "actions_log disabled: no entries should be written")

    def test_catalog_snapshot_off_leaves_snapshot_untouched(self) -> None:
        set_feature_flag(self.paths, "catalog_snapshot", False)
        # Pre-seed snapshot with a sentinel so we can detect any rewrite.
        self.paths.catalog_snapshot_file.write_text("SENTINEL", encoding="utf-8")
        (self.paths.inbox_dir / "doc.pdf").write_bytes(b"content")

        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")
        self.assertEqual(
            self.paths.catalog_snapshot_file.read_text(encoding="utf-8"),
            "SENTINEL",
            "catalog_snapshot disabled: file must not be overwritten",
        )

    def test_mirror_sync_off_skips_mirror_copy(self) -> None:
        set_feature_flag(self.paths, "mirror_sync", False)
        (self.paths.inbox_dir / "doc.pdf").write_bytes(b"content")

        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")

        # Library has the file, mirror does not.
        library_files = [p for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertEqual(len(library_files), 1)
        mirror_files = [p for p in self.paths.mirror_root.rglob("*") if p.is_file()]
        self.assertEqual(mirror_files, [], "mirror_sync disabled: no file should be copied")

        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(any(e["action"] == "mirror_sync_skipped" for e in events))


if __name__ == "__main__":
    unittest.main()
