import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from procrafiler.catalog import CatalogRepository


class TestCatalog(unittest.TestCase):
    def test_duplicate_detection_by_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.db"
            repo = CatalogRepository(db_path)
            repo.init_schema()

            repo.upsert_document(
                doc_id="doc-1",
                sha256="abc123",
                current_filename="2026-04-01_22-10-06__file.pdf",
                current_path="/library/file.pdf",
                status="LIBRARY_STORED",
                updated_at_utc="2026-04-01T22:10:06Z",
            )

            self.assertTrue(repo.has_sha256("abc123"))
            self.assertFalse(repo.has_sha256("def456"))

    def test_tombstone_reduces_row_to_id_hash_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = CatalogRepository(Path(tmp) / "catalog.db")
            repo.init_schema()
            repo.upsert_document(
                doc_id="doc-1",
                sha256="abc123",
                current_filename="2026-04-01_22-10-06__file.pdf",
                current_path="/library/Personal/file.pdf",
                status="LIBRARY_STORED",
                updated_at_utc="2026-04-01T22:10:06Z",
                content_json='{"name": "secret"}',
            )

            # A live document is a duplicate and is not a tombstone.
            self.assertTrue(repo.has_live_sha256("abc123"))
            self.assertIsNone(repo.deleted_at_for_sha256("abc123"))

            repo.tombstone_document("doc-1", sha256="abc123", deleted_at="2026-06-01T00:00:00Z")

            # The hash survives (for re-deposit recognition); nothing else does.
            self.assertTrue(repo.has_sha256("abc123"))  # row still exists
            self.assertFalse(repo.has_live_sha256("abc123"))  # but no longer a duplicate
            self.assertEqual(repo.deleted_at_for_sha256("abc123"), "2026-06-01T00:00:00Z")
            self.assertIsNone(repo.find_by_current_path("/library/Personal/file.pdf"))

            row = next(d for d in repo.list_documents() if d["doc_id"] == "doc-1")
            self.assertEqual(row["status"], "DELETED")
            self.assertEqual(row["current_filename"], "")
            self.assertEqual(row["current_path"], "")
            self.assertIsNone(row["content_json"])

    def test_init_schema_migrates_an_old_catalog(self) -> None:
        # An update must read a catalog written by an OLDER version: init_schema
        # adds the newer columns to the existing table without touching the rows.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "catalog.db"
            conn = sqlite3.connect(db)  # a v0.1-era catalog: only the original 6 columns
            conn.execute(
                "CREATE TABLE documents (doc_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                "current_filename TEXT NOT NULL, current_path TEXT NOT NULL, status TEXT NOT NULL, "
                "updated_at_utc TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO documents VALUES ('d1', 'abc', 'f.pdf', '/lib/f.pdf', 'LIBRARY_STORED', '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            conn.close()

            repo = CatalogRepository(db)
            repo.init_schema()  # the "update" opening the old catalog

            cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(documents)").fetchall()}
            self.assertTrue({"flow_state", "pending_decision", "content_json"} <= cols)  # migrated
            self.assertTrue(repo.has_sha256("abc"))  # old row survived
            row = repo.find_by_current_path("/lib/f.pdf")
            assert row is not None
            self.assertEqual(row["doc_id"], "d1")
            self.assertIsNone(row["content_json"])  # new column defaults to NULL

    def test_majority_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = CatalogRepository(Path(tmp) / "catalog.db")
            repo.init_schema()
            self.assertIsNone(repo.majority_language())  # empty catalog
            for doc_id, lang in (("a", "fr"), ("b", "fr"), ("c", "en")):
                repo.upsert_document(
                    doc_id=doc_id, sha256=doc_id, current_filename=f"{doc_id}.txt",
                    current_path=f"/l/{doc_id}.txt", status="LIBRARY_STORED",
                    updated_at_utc="2026-01-01T00:00:00Z", content_json=json.dumps({"language": lang}),
                )
            self.assertEqual(repo.majority_language(), "fr")  # 2 fr vs 1 en

    def test_list_pending_decisions_filters_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = CatalogRepository(Path(tmp) / "catalog.db")
            repo.init_schema()

            repo.upsert_document(
                doc_id="settled",
                sha256="aaa",
                current_filename="a.pdf",
                current_path="/library/Personal/Administrative/Banking/a.pdf",
                status="LIBRARY_STORED",
                updated_at_utc="2026-04-01T10:00:00Z",
            )
            repo.upsert_document(
                doc_id="parked",
                sha256="bbb",
                current_filename="b.pdf",
                current_path="/library/Manual_Review/b.pdf",
                status="DECISION_PENDING",
                updated_at_utc="2026-04-01T11:00:00Z",
                pending_decision='{"options": ["Personal/Administrative/Banking", "Personal/Administrative"]}',
            )

            pending = repo.list_pending_decisions()
            self.assertEqual([row["doc_id"] for row in pending], ["parked"])
            self.assertEqual(pending[0]["pending_decision"], '{"options": ["Personal/Administrative/Banking", "Personal/Administrative"]}')

            # Resolving clears the flag and drops it out of the queue.
            repo.upsert_document(
                doc_id="parked",
                sha256="bbb",
                current_filename="b.pdf",
                current_path="/library/Personal/Administrative/Banking/b.pdf",
                status="LIBRARY_STORED",
                updated_at_utc="2026-04-01T12:00:00Z",
                pending_decision=None,
            )
            self.assertEqual(repo.list_pending_decisions(), [])


if __name__ == "__main__":
    unittest.main()
