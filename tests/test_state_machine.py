# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_next_inbox_file


class TestStateMachinePersistence(unittest.TestCase):
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

    def _flow_state_of(self, sha256: str) -> str | None:
        with sqlite3.connect(self.paths.catalog_db_file) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT flow_state FROM documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
            return row["flow_state"] if row else None

    def test_library_stored_persists_flow_state(self) -> None:
        (self.paths.inbox_dir / "doc.pdf").write_bytes(b"content")
        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")

        repo = CatalogRepository(self.paths.catalog_db_file)
        doc = next(d for d in repo.list_documents() if d["status"] == "LIBRARY_STORED")
        self.assertEqual(doc["flow_state"], "LIBRARY_STORED")

    def test_manual_review_persists_flow_state(self) -> None:
        (self.paths.inbox_dir / "weird.unknownext").write_bytes(b"data")
        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "USER_CONFIRMATION_REQUIRED")

        repo = CatalogRepository(self.paths.catalog_db_file)
        doc = next(iter(repo.list_documents()))
        self.assertEqual(doc["flow_state"], "USER_CONFIRMATION_REQUIRED")

    def test_duplicate_path_does_not_persist_a_document(self) -> None:
        (self.paths.inbox_dir / "first.pdf").write_bytes(b"same-content")
        process_next_inbox_file(self.paths, now_utc=self.now)

        (self.paths.inbox_dir / "second.pdf").write_bytes(b"same-content")
        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "INBOX_TRASH_PENDING_MANUAL")

        repo = CatalogRepository(self.paths.catalog_db_file)
        docs = repo.list_documents()
        self.assertEqual(len(docs), 1, "duplicate must not create a second row")

    def test_legacy_db_without_flow_state_column_is_migrated(self) -> None:
        legacy_db = self.paths.catalog_db_file
        # Wipe the auto-created file so we can pretend we're upgrading an
        # older install that predates the flow_state column.
        legacy_db.unlink(missing_ok=True)
        with sqlite3.connect(legacy_db) as conn:
            conn.execute(
                """
                CREATE TABLE documents (
                    doc_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    current_filename TEXT NOT NULL,
                    current_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                ("legacy-1", "abc", "old.pdf", "/lib/old.pdf", "LIBRARY_STORED", "2025-01-01T00:00:00Z"),
            )
            conn.commit()

        repo = CatalogRepository(legacy_db)
        repo.init_schema()

        with sqlite3.connect(legacy_db) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        self.assertIn("flow_state", columns)

        # Legacy row's flow_state stays NULL until the row is touched again.
        with sqlite3.connect(legacy_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT flow_state FROM documents WHERE doc_id = ?", ("legacy-1",)).fetchone()
        self.assertIsNone(row["flow_state"])


if __name__ == "__main__":
    unittest.main()
