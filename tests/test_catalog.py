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
