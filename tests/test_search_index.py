from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.cli import main
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.search import reindex_content, search_catalog
from procrafiler.search_index import BodyTextIndex


class TestBodyTextIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.idx = BodyTextIndex(Path(self.tmp.name) / "search_index.db")
        self.idx.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_put_get_delete(self) -> None:
        self.idx.put("a", "alpha body", now_utc_iso="2026-01-01T00:00:00Z")
        self.idx.put("b", "", now_utc_iso="2026-01-01T00:00:00Z")  # empty body is a valid cached result
        self.assertEqual(self.idx.get_many(["a", "b", "c"]), {"a": "alpha body", "b": ""})
        self.idx.delete("a")
        self.assertEqual(self.idx.get_many(["a"]), {})
        self.assertEqual(self.idx.count(), 1)

    def test_put_overwrites(self) -> None:
        self.idx.put("a", "v1", now_utc_iso="2026-01-01T00:00:00Z")
        self.idx.put("a", "v2", now_utc_iso="2026-01-02T00:00:00Z")
        self.assertEqual(self.idx.get_many(["a"]), {"a": "v2"})

    def test_prune_keeps_only_listed(self) -> None:
        for s in ("a", "b", "c"):
            self.idx.put(s, s, now_utc_iso="2026-01-01T00:00:00Z")
        self.assertEqual(self.idx.prune({"b"}), 2)
        self.assertEqual(self.idx.all_shas(), {"b"})


class TestReindexAndWarming(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "catalog.db"
        self.index = self.root / "search_index.db"
        self.repo = CatalogRepository(self.db)
        self.repo.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _doc(self, doc_id: str, *, filename: str, body: str) -> Path:
        f = self.root / filename
        f.write_text(body, encoding="utf-8")
        self.repo.upsert_document(
            doc_id=doc_id, sha256=doc_id, current_filename=f.name, current_path=str(f),
            status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
            content_json=json.dumps({"name": filename}),
        )
        return f

    def test_reindex_backfills_then_prunes_and_is_cheap_to_rerun(self) -> None:
        self._doc("d1", filename="d1.txt", body="convention metallurgie")
        counts = reindex_content(self.db, index_path=self.index)
        self.assertEqual((counts["added"], counts["indexed"], counts["pruned"]), (1, 1, 0))
        self.assertIn("metallurgie", BodyTextIndex(self.index).get_many(["d1"])["d1"])

        # Re-run: already indexed → nothing read again.
        self.assertEqual(reindex_content(self.db, index_path=self.index)["added"], 0)

        # Content no longer in the library → its body is pruned from the index.
        self.repo.purge_document("d1")
        counts3 = reindex_content(self.db, index_path=self.index)
        self.assertEqual((counts3["added"], counts3["pruned"], counts3["indexed"]), (0, 1, 0))

    def test_search_warms_the_index_then_reads_from_it(self) -> None:
        f = self._doc("d1", filename="d1.txt", body="zogzog deep in the body")
        # First search warms the cache from disk.
        self.assertEqual([h.doc_id for h in search_catalog(self.db, "zogzog", index_path=self.index)], ["d1"])
        self.assertIn("d1", BodyTextIndex(self.index).all_shas())

        # Delete the file on disk — the body now lives ONLY in the persistent index.
        f.unlink()
        self.assertEqual([h.doc_id for h in search_catalog(self.db, "zogzog", index_path=self.index)], ["d1"])


class TestReindexCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reindex_cli_reports_counts(self) -> None:
        doc = self.paths.library_root / "Personal" / "note.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("alpha beta gamma", encoding="utf-8")
        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        repo.upsert_document(
            doc_id="n1", sha256="n1", current_filename=doc.name, current_path=str(doc),
            status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z", content_json="{}",
        )
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["reindex"]), 0)
        self.assertIn("indexed: 1", out.getvalue())
        self.assertTrue(self.paths.search_index_file.exists())


if __name__ == "__main__":
    unittest.main()
