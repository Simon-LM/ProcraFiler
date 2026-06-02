# pyright: reportUnknownVariableType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_next_inbox_file  # type: ignore[reportMissingImports]


class TestPipeline(unittest.TestCase):
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

    def test_process_new_file_to_interim_review(self) -> None:
        # Until AI classification exists, a readable file (known extension) is
        # ingested into the interim review directory (Revue_Manuelle) rather
        # than a wrongly extension-derived category. The extension only told us
        # how to read it, not where it belongs.
        source = self.paths.inbox_dir / "my doc.pdf"
        source.write_bytes(b"hello-world")
        # No AI chain here, so the filename date comes from the file's mtime
        # (the content-date cascade). Pin it so the prefix is deterministic.
        mtime = datetime(2026, 4, 2, 10, 11, 12, tzinfo=timezone.utc).timestamp()
        os.utime(source, (mtime, mtime))

        status: str = process_next_inbox_file(
            self.paths, now_utc=datetime(2026, 4, 2, 10, 11, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(status, "LIBRARY_STORED")

        # The file must NOT land in an extension-derived semantic category.
        self.assertFalse((self.paths.library_root / "Personnel" / "Documents").exists()
                         and list((self.paths.library_root / "Personnel" / "Documents").iterdir()))

        target_dir = self.paths.library_root / "Revue_Manuelle"
        self.assertTrue(target_dir.exists())
        files = list(target_dir.iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("2026-04-02_10-11-12__"))

        mirror_target_dir = self.paths.mirror_root / "Revue_Manuelle"
        self.assertTrue(mirror_target_dir.exists())
        mirror_files = list(mirror_target_dir.iterdir())
        self.assertEqual(len(mirror_files), 1)
        self.assertEqual(mirror_files[0].name, files[0].name)

        src_hash = hashlib.sha256(files[0].read_bytes()).hexdigest()
        mirror_hash = hashlib.sha256(mirror_files[0].read_bytes()).hexdigest()
        self.assertEqual(src_hash, mirror_hash)

        repo = CatalogRepository(self.paths.catalog_db_file)
        self.assertTrue(repo.has_sha256("afa27b44d43b02a9fea41d13cedc2e4016cfcf87c5dbf990e593669aa8ce286d"))

        snapshot = json.loads(self.paths.catalog_snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["meta"]["documents_count"], 1)

        lines = [line for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 6)
        events = [json.loads(line) for line in lines]
        self.assertTrue(any(e["action"] == "mirror_sync_success" for e in events))
        # The fake PDF has no text layer and there is no OCR chain, so the content
        # is unreadable and the unified analysis never runs.
        self.assertTrue(any(e["action"] == "ocr_read_unavailable" for e in events))
        # The move records the technical media type, not a semantic category.
        move_events = [e for e in events if e["action"] == "move_to_library"]
        self.assertEqual(len(move_events), 1)
        self.assertEqual(move_events[0]["media_type"], "pdf")
        self.assertEqual(move_events[0]["target_route"], "Revue_Manuelle")
        # The pipeline now reads the content locally before storing. The fake
        # PDF bytes here have no valid text layer, so the reader flags OCR.
        content_events = [e for e in events if e["action"] == "content_read"]
        self.assertEqual(len(content_events), 1)
        self.assertEqual(content_events[0]["media_type"], "pdf")
        self.assertTrue(content_events[0]["needs_ai_reader"])
        self.assertEqual(content_events[0]["reader_hint"], "ocr")

    def test_process_duplicate_to_inbox_trash_manual(self) -> None:
        first = self.paths.inbox_dir / "doc-a.pdf"
        first.write_bytes(b"duplicate-content")
        process_next_inbox_file(self.paths, now_utc=datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc))

        second = self.paths.inbox_dir / "doc-b.pdf"
        second.write_bytes(b"duplicate-content")

        status: str = process_next_inbox_file(
            self.paths, now_utc=datetime(2026, 4, 2, 10, 1, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(status, "INBOX_TRASH_PENDING_MANUAL")

        trash_files = list(self.paths.inbox_trash_manual_dir.iterdir())
        self.assertEqual(len(trash_files), 1)

        snapshot = json.loads(self.paths.catalog_snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["meta"]["documents_count"], 1)

        mirror_files = [p for p in self.paths.mirror_root.rglob("*") if p.is_file()]
        self.assertEqual(len(mirror_files), 1)

    def test_unknown_extension_stays_in_queue_with_manual_alert(self) -> None:
        source = self.paths.inbox_dir / "archive.weirdext"
        source.write_bytes(b"custom-binary")

        status: str = process_next_inbox_file(
            self.paths, now_utc=datetime(2026, 4, 2, 10, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(status, "USER_CONFIRMATION_REQUIRED")

        self.assertEqual(len(list(self.paths.inbox_dir.iterdir())), 0)
        queue_files = list(self.paths.queue_dir.iterdir())
        self.assertEqual(len(queue_files), 1)
        self.assertEqual(queue_files[0].name, "archive.weirdext")

        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        manual_events = [e for e in events if e.get("action") == "manual_review_required"]
        self.assertTrue(manual_events)
        self.assertEqual(manual_events[0].get("reason"), "unknown_extension")

    def test_no_extension_stays_in_queue_with_manual_alert(self) -> None:
        source = self.paths.inbox_dir / "README"
        source.write_bytes(b"no-extension")

        status: str = process_next_inbox_file(
            self.paths, now_utc=datetime(2026, 4, 2, 10, 13, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(status, "USER_CONFIRMATION_REQUIRED")

        queue_files = list(self.paths.queue_dir.iterdir())
        self.assertEqual(len(queue_files), 1)
        self.assertEqual(queue_files[0].name, "README")

        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        manual_events = [e for e in events if e.get("action") == "manual_review_required"]
        self.assertTrue(manual_events)
        self.assertEqual(manual_events[0].get("reason"), "no_extension")


if __name__ == "__main__":
    unittest.main()
