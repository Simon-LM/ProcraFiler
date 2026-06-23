from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from procrafiler.ai_analysis import expand_query
from procrafiler.ai_naming import ChainEntry
from procrafiler.catalog import CatalogRepository
from procrafiler.cli import main
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.search import search_catalog_any


class TestExpandQuery(unittest.TestCase):
    def test_returns_related_terms(self) -> None:
        chain = [ChainEntry("mistral", "m")]
        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value='{"keywords": ["acoustique", "sound", "audio", "son"]}',
        ):
            out = expand_query("acoustic", language="fr", chain=chain)
        self.assertEqual(out, ["acoustique", "sound", "audio", "son"])

    def test_works_for_english_too(self) -> None:
        # Unlike translate_keywords, query expansion is useful in English (synonyms).
        chain = [ChainEntry("mistral", "m")]
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value='{"keywords": ["boat", "ship"]}'):
            self.assertEqual(expand_query("boat", language="en", chain=chain), ["boat", "ship"])

    def test_empty_query_or_no_chain(self) -> None:
        self.assertEqual(expand_query("", language="fr", chain=[ChainEntry("mistral", "m")]), [])
        self.assertEqual(expand_query("acoustic", language="fr", chain=[]), [])


class TestSearchAny(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "catalog.db"
        self.repo = CatalogRepository(self.db)
        self.repo.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _doc(self, doc_id: str, fiche: dict) -> None:
        self.repo.upsert_document(
            doc_id=doc_id, sha256=doc_id, current_filename=f"{doc_id}.pdf",
            current_path=f"/lib/{doc_id}.pdf", status="LIBRARY_STORED",
            updated_at_utc="2026-01-01T00:00:00Z", content_json=json.dumps(fiche),
        )

    def test_matches_any_of_the_terms(self) -> None:
        self._doc("a", {"name": "Doc-A", "keywords": ["acoustique"]})
        self._doc("b", {"name": "Doc-B", "keywords": ["banque"]})
        self.assertEqual({h.doc_id for h in search_catalog_any(self.db, ["acoustique", "banque"])}, {"a", "b"})
        self.assertEqual([h.doc_id for h in search_catalog_any(self.db, ["banque"])], ["b"])
        self.assertEqual(search_catalog_any(self.db, ["licorne"]), [])

    def test_empty_terms_returns_nothing(self) -> None:
        self._doc("a", {"name": "A", "keywords": ["x"]})
        self.assertEqual(search_catalog_any(self.db, []), [])

    def test_drops_stopwords_to_avoid_grammatical_noise(self) -> None:
        # "son" (a French possessive) must not pull in documents that merely
        # contain it; the real synonym "audio" still matches.
        self._doc("audio", {"name": "Manuel", "keywords": ["audio"]})
        self._doc("noise", {"name": "Lettre", "summary": "il envoie à son assureur"})
        self.assertEqual([h.doc_id for h in search_catalog_any(self.db, ["son", "audio"])], ["audio"])
        self.assertEqual(search_catalog_any(self.db, ["son", "de", "le"]), [])  # all stopwords


class TestSearchAiCli(unittest.TestCase):
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
        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        repo.upsert_document(
            doc_id="a", sha256="a", current_filename="a.pdf",
            current_path=str(self.paths.library_root / "a.pdf"), status="LIBRARY_STORED",
            updated_at_utc="2026-01-01T00:00:00Z",
            content_json=json.dumps({"name": "Manuel", "keywords": ["acoustique"]}),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_expands_with_ai_then_finds(self) -> None:
        # Query is English "acoustic"; the AI expansion includes the French keyword.
        with patch("procrafiler.ai_analysis.expand_query", return_value=["acoustique", "son"]):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["search-ai", "acoustic"]), 0)
        text = out.getvalue()
        self.assertIn("acoustique", text)       # the expansion is shown
        self.assertIn("result(s)", text)         # and the document is found

    def test_no_ai_falls_back_to_plain_search(self) -> None:
        with patch("procrafiler.ai_analysis.expand_query", return_value=[]):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["search-ai", "acoustique"]), 0)
        text = out.getvalue()
        self.assertIn("unavailable", text)       # fallback notice
        self.assertIn("result(s)", text)         # plain search still finds it


if __name__ == "__main__":
    unittest.main()
