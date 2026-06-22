from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.search import search_catalog


class TestSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "catalog.db"
        self.repo = CatalogRepository(self.db)
        self.repo.init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _doc(self, doc_id: str, fiche: dict, *, status: str = "LIBRARY_STORED") -> None:
        self.repo.upsert_document(
            doc_id=doc_id, sha256=doc_id, current_filename=f"{doc_id}.pdf",
            current_path=f"/lib/{doc_id}.pdf", status=status,
            updated_at_utc="2026-01-01T00:00:00Z", content_json=json.dumps(fiche),
        )

    def _doc_with_body(self, doc_id: str, fiche: dict, *, filename: str,
                       body: str | None = None, sidecar_text: str | None = None) -> Path:
        """A document backed by a real file on disk (so body text can be read),
        optionally with a hidden OCR/vision sidecar."""
        f = Path(self.tmp.name) / filename
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8") if body is not None else f.write_bytes(b"%PDF-noop")
        if sidecar_text is not None:
            (f.parent / ("." + f.name + ".txt")).write_text(sidecar_text, encoding="utf-8")
        self.repo.upsert_document(
            doc_id=doc_id, sha256=doc_id, current_filename=f.name, current_path=str(f),
            status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
            content_json=json.dumps(fiche),
        )
        return f

    def test_finds_by_keyword_and_entity(self) -> None:
        self._doc("edf", {"name": "Facture_EDF", "keywords": ["facture", "electricite"],
                          "entities": {"issuer": "EDF"}, "summary": "Facture d'électricité.",
                          "category_path": "Personal/Administrative/Utilities/EDF"})
        self._doc("bnp", {"name": "Releve_BNP-Paribas", "keywords": ["banque", "releve"],
                          "entities": {"issuer": "BNP Paribas"}, "summary": "Relevé de compte."})
        hits = search_catalog(self.db, "facture edf")
        self.assertEqual([h.doc_id for h in hits], ["edf"])
        self.assertEqual(hits[0].category_path, "Personal/Administrative/Utilities/EDF")

    def test_accent_insensitive(self) -> None:
        self._doc("impots", {"name": "Avis_Impots", "keywords": ["impôt", "fisc"],
                             "summary": "Avis d'impôt sur le revenu."})
        # typed without the accent → still matches
        self.assertEqual([h.doc_id for h in search_catalog(self.db, "impot")], ["impots"])

    def test_name_outranks_summary(self) -> None:
        self._doc("a", {"name": "Voyage-Espagne", "summary": "des notes diverses"})
        self._doc("b", {"name": "Notes", "summary": "un voyage en Espagne raconté"})
        hits = search_catalog(self.db, "voyage")
        self.assertEqual(hits[0].doc_id, "a")  # match in the name ranks first

    def test_deleted_documents_are_not_searched(self) -> None:
        self._doc("gone", {"name": "Facture_EDF", "keywords": ["facture"]}, status="DELETED")
        self.assertEqual(search_catalog(self.db, "facture"), [])

    def test_finds_by_sidecar_body(self) -> None:
        # The fiche says nothing about cardiology; the cached OCR sidecar does.
        self._doc_with_body("scan", {"name": "Scan_2025", "keywords": [], "summary": ""},
                            filename="scan.pdf", sidecar_text="Compte rendu : rendez-vous avec le cardiologue.")
        hits = search_catalog(self.db, "cardiologue")
        self.assertEqual([h.doc_id for h in hits], ["scan"])
        self.assertIn("cardiologue", hits[0].snippet.lower())

    def test_finds_by_plain_text_body_accent_insensitive(self) -> None:
        # A word only in the document body (a real .txt file), typed without accent.
        self._doc_with_body("cc", {"name": "Doc", "keywords": [], "summary": ""},
                            filename="cc.txt", body="Convention collective de la métallurgie.")
        self.assertEqual([h.doc_id for h in search_catalog(self.db, "metallurgie")], ["cc"])

    def test_fiche_name_outranks_body_only_match(self) -> None:
        self._doc_with_body("named", {"name": "Voyage-Espagne", "summary": ""},
                            filename="named.txt", body="rien de pertinent ici")
        self._doc_with_body("bodied", {"name": "Notes", "summary": ""},
                            filename="bodied.txt", body="un grand voyage raconté")
        hits = search_catalog(self.db, "voyage")
        self.assertEqual(hits[0].doc_id, "named")  # match in the name outranks a body-only match

    def test_scanned_doc_without_sidecar_contributes_only_its_fiche(self) -> None:
        # An unreadable PDF with no sidecar: body is empty, only the fiche matches.
        self._doc_with_body("scanned", {"name": "Bulletin", "keywords": ["paie"], "summary": ""},
                            filename="scanned.pdf")  # bytes, no text layer, no sidecar
        self.assertEqual([h.doc_id for h in search_catalog(self.db, "paie")], ["scanned"])
        self.assertEqual(search_catalog(self.db, "licorne"), [])

    def test_empty_query_returns_nothing(self) -> None:
        self._doc("edf", {"name": "Facture_EDF", "keywords": ["facture"]})
        self.assertEqual(search_catalog(self.db, "   "), [])

    def test_no_match_returns_empty(self) -> None:
        self._doc("edf", {"name": "Facture_EDF", "keywords": ["facture"]})
        self.assertEqual(search_catalog(self.db, "licorne"), [])


if __name__ == "__main__":
    unittest.main()
