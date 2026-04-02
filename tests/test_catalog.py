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


if __name__ == "__main__":
    unittest.main()
