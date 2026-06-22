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
        # ingested into the interim review directory (Manual_Review) rather
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

        # The file must NOT land in a real (extension-derived) category — being
        # unreadable, it goes to Manual_Review. No file under Personal / Work.
        filed_in_real_category = [
            p
            for top in ("Personal", "Work")
            for p in (self.paths.library_root / top).rglob("*")
            if p.is_file()
        ]
        self.assertEqual(filed_in_real_category, [])

        target_dir = self.paths.library_root / "Manual_Review"
        self.assertTrue(target_dir.exists())
        files = list(target_dir.iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("2026-04-02_10-11-12__"))

        mirror_target_dir = self.paths.mirror_root / "Manual_Review"
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
        self.assertEqual(move_events[0]["target_route"], "Manual_Review")
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

    def test_redeposit_of_a_deleted_file_is_not_trashed(self) -> None:
        # run-19: deleting a doc left a tombstone whose hash matched the inbox →
        # re-adding it was wrongly trashed as a duplicate. A tombstone (deleted)
        # must NOT block a re-deposit; it is re-filed, and the re-deposit is logged.
        from procrafiler.catalog import CatalogRepository
        from procrafiler.pipeline import _file_sha256

        f = self.paths.inbox_dir / "again.txt"
        f.write_bytes(b"some content I deleted then re-added")
        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        repo.upsert_document(
            doc_id="t1", sha256=_file_sha256(f), current_filename="x", current_path="/old/x",
            status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
        )
        repo.tombstone_document("t1", sha256=_file_sha256(f), deleted_at="2026-06-01T00:00:00Z")

        status = process_next_inbox_file(self.paths, now_utc=datetime(2026, 6, 20, 9, 0, 0, tzinfo=timezone.utc))
        self.assertNotEqual(status, "INBOX_TRASH_PENDING_MANUAL")  # not a duplicate
        self.assertEqual(len(list(self.paths.inbox_trash_manual_dir.iterdir())), 0)
        actions = [
            json.loads(line)["action"]
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        self.assertIn("redeposit_of_deleted", actions)

    def _manual_review_files(self) -> list[Path]:
        review_dir = self.paths.library_root / "Manual_Review"
        return [p for p in review_dir.rglob("*") if p.is_file()] if review_dir.exists() else []

    def test_unknown_extension_is_filed_in_manual_review(self) -> None:
        # An unsupported extension can't be read, so it goes to Manual_Review
        # (the catch-all for unreadable content) — NOT stranded in the Queue.
        source = self.paths.inbox_dir / "archive.weirdext"
        source.write_bytes(b"custom-binary")

        status: str = process_next_inbox_file(
            self.paths, now_utc=datetime(2026, 4, 2, 10, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(status, "LIBRARY_STORED")

        self.assertEqual(len(list(self.paths.inbox_dir.iterdir())), 0)
        self.assertEqual(list(self.paths.queue_dir.iterdir()), [])  # nothing left behind
        review_files = self._manual_review_files()
        self.assertEqual(len(review_files), 1)
        self.assertTrue(review_files[0].name.endswith("archive.weirdext"))

        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        manual_events = [e for e in events if e.get("action") == "manual_review_required"]
        self.assertTrue(manual_events)
        self.assertEqual(manual_events[0].get("reason"), "unknown_extension")

    def test_no_extension_is_filed_in_manual_review(self) -> None:
        source = self.paths.inbox_dir / "README"
        source.write_bytes(b"no-extension")

        status: str = process_next_inbox_file(
            self.paths, now_utc=datetime(2026, 4, 2, 10, 13, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(status, "LIBRARY_STORED")

        self.assertEqual(list(self.paths.queue_dir.iterdir()), [])
        review_files = self._manual_review_files()
        self.assertEqual(len(review_files), 1)
        self.assertTrue(review_files[0].name.endswith("README"))

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
