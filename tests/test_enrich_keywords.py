from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from procrafiler.ai_naming import ChainEntry
from procrafiler.catalog import CatalogRepository
from procrafiler.cli import main
from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    set_user_language,
)


class TestTranslateKeywords(unittest.TestCase):
    def test_returns_translations_for_a_user_language(self) -> None:
        from procrafiler.ai_analysis import translate_keywords
        chain = [ChainEntry("mistral", "m")]
        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value='{"keywords": ["boat", "ship", "navire"]}',
        ):
            out = translate_keywords(["bateau"], language="fr", chain=chain)
        self.assertEqual(out, ["boat", "ship", "navire"])

    def test_noop_for_english_or_empty(self) -> None:
        from procrafiler.ai_analysis import translate_keywords
        chain = [ChainEntry("mistral", "m")]
        self.assertEqual(translate_keywords(["x"], language="en", chain=chain), [])
        self.assertEqual(translate_keywords([], language="fr", chain=chain), [])

    def test_no_chain_returns_empty(self) -> None:
        from procrafiler.ai_analysis import translate_keywords
        self.assertEqual(translate_keywords(["bateau"], language="fr", chain=[]), [])


class TestEnrichKeywordsMigration(unittest.TestCase):
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
        self.repo = CatalogRepository(self.paths.catalog_db_file)
        self.repo.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _doc(self, doc_id: str, fiche: dict) -> None:
        self.repo.upsert_document(
            doc_id=doc_id, sha256=doc_id, current_filename=f"{doc_id}.txt",
            current_path=f"/lib/{doc_id}.txt", status="LIBRARY_STORED",
            updated_at_utc="2026-01-01T00:00:00Z", content_json=json.dumps(fiche),
        )

    def _fiche(self, doc_id: str) -> dict:
        row = self.repo.find_by_current_path(f"/lib/{doc_id}.txt")
        assert row is not None
        return json.loads(str(row["content_json"]))

    def test_enriches_then_is_idempotent(self) -> None:
        from procrafiler.pipeline import enrich_keywords
        set_user_language(self.paths, "fr")
        self._doc("d1", {"name": "Truc", "keywords": ["bateau"], "summary": ""})

        with patch("procrafiler.pipeline.translate_keywords", return_value=["boat", "ship"]):
            counts = enrich_keywords(self.paths)
        self.assertEqual(counts["enriched"], 1)
        fiche = self._fiche("d1")
        self.assertEqual(fiche["keywords"], ["bateau", "boat", "ship"])  # merged, originals kept
        self.assertTrue(fiche["keywords_enriched"])

        # Re-run: already enriched → skipped, no AI call.
        with patch("procrafiler.pipeline.translate_keywords", side_effect=AssertionError("should not call")):
            counts2 = enrich_keywords(self.paths)
        self.assertEqual((counts2["enriched"], counts2["skipped"]), (0, 1))

    def test_english_language_is_a_noop(self) -> None:
        from procrafiler.pipeline import enrich_keywords
        self._doc("d1", {"name": "Thing", "keywords": ["boat"], "summary": ""})
        with patch("procrafiler.pipeline.translate_keywords", side_effect=AssertionError("should not call")):
            counts = enrich_keywords(self.paths)  # default language is English
        self.assertEqual(counts["enriched"], 0)
        self.assertNotIn("keywords_enriched", self._fiche("d1"))

    def test_cli_reports_counts(self) -> None:
        set_user_language(self.paths, "fr")
        self._doc("d1", {"name": "Truc", "keywords": ["bateau"], "summary": ""})
        out = io.StringIO()
        with patch("procrafiler.pipeline.translate_keywords", return_value=["boat"]), redirect_stdout(out):
            self.assertEqual(main(["enrich-keywords"]), 0)
        self.assertIn("enriched: 1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
