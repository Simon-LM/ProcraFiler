# pyright: reportUnknownVariableType=false
"""A photographed DOCUMENT is re-read with OCR instead of merely described.

A photo dispatches to the vision model because it is a `.jpg` — even when it *is*
a document. So a photographed invoice used to come back as "an administrative
document with a logo" instead of its amount, reference and date. That weak text
is then cached in the search sidecar, so the loss is permanent, not just one bad
filing decision.

The vision model now ends its answer with `DOCUMENT: oui|non`. On `oui` the file
is re-read with the OCR model, and BOTH texts are kept — the OCR transcription
first and labelled reliable, the visual description after as context.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import procrafiler.ai_reader as ai_reader
import procrafiler.pipeline as pipeline
from procrafiler.ai_reader import split_document_marker
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _sidecar_path, process_all_inbox_files


def _read(text, *, is_document=None, reason=None):
    """Stand in for an AIReadResult without importing the frozen dataclass."""
    return type(
        "R", (), {"text": text, "provider": "p", "model": "m", "reason": reason,
                  "used_fallback": text is None, "is_document": is_document}
    )()


class TestDocumentMarker(unittest.TestCase):
    """The marker must never leak into the text that gets cached and searched."""

    def test_oui_is_detected_and_stripped(self) -> None:
        text, flag = split_document_marker("FACTURE EDF 87,40 EUR\nDOCUMENT: oui")
        self.assertTrue(flag)
        self.assertEqual(text, "FACTURE EDF 87,40 EUR")
        self.assertNotIn("DOCUMENT", text)

    def test_non_is_detected_and_stripped(self) -> None:
        text, flag = split_document_marker("Un chat sur un canape.\nDOCUMENT: non")
        self.assertFalse(flag)
        self.assertEqual(text, "Un chat sur un canape.")

    def test_a_missing_marker_degrades_to_unknown(self) -> None:
        """A local model may ignore the question. That must mean "don't re-read",
        never a crash and never a stray line in the cached text."""
        text, flag = split_document_marker("Un chat sur un canape.")
        self.assertIsNone(flag)
        self.assertEqual(text, "Un chat sur un canape.")

    def test_case_and_spacing_are_tolerated(self) -> None:
        for raw in ("x\nDOCUMENT: OUI", "x\ndocument :  oui  ", "x\nDOCUMENT: yes"):
            with self.subTest(raw=raw):
                text, flag = split_document_marker(raw)
                self.assertTrue(flag, raw)
                self.assertEqual(text, "x")

    def test_the_last_marker_wins(self) -> None:
        """The instruction asks for a FINAL line; a model that also mentions the
        format mid-answer must not flip the verdict."""
        _text, flag = split_document_marker("DOCUMENT: non\nblabla\nDOCUMENT: oui")
        self.assertTrue(flag)


class TestOcrConfirmInPipeline(unittest.TestCase):
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
        self.now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _drop_photo(self) -> None:
        (self.paths.inbox_dir / "IMG_2024.jpg").write_bytes(b"\xff\xd8\xff\xe0 not-a-real-jpeg")

    def _run(self, vision, ocr):
        self._drop_photo()
        with patch.object(ai_reader, "read_with_vision", return_value=vision) as v:
            with patch.object(ai_reader, "read_with_ocr", return_value=ocr) as o:
                process_all_inbox_files(self.paths, now_utc=self.now)
        return v, o

    def _filed(self) -> Path:
        files = [
            p for p in self.paths.library_root.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        ]
        self.assertEqual(len(files), 1, "expected exactly one filed document")
        return files[0]

    def test_a_photographed_document_is_re_read_with_ocr(self) -> None:
        vision = _read("Photo d'une facture posee sur une table.", is_document=True)
        ocr = _read("FACTURE N 2026-0412 — EDF — Montant TTC : 87,40 EUR")
        _v, ocr_call = self._run(vision, ocr)

        ocr_call.assert_called_once()
        sidecar = _sidecar_path(self._filed())
        cached = sidecar.read_text(encoding="utf-8")
        # BOTH texts are kept, the OCR first and labelled reliable.
        self.assertIn("87,40 EUR", cached, "the OCR transcription was not cached")
        self.assertIn("posee sur une table", cached, "the visual context was dropped")
        self.assertLess(
            cached.index("Transcription OCR"), cached.index("Description visuelle"),
            "the OCR text must come first — order carries the weighting",
        )

    def test_a_plain_photo_is_not_sent_to_ocr(self) -> None:
        """A photo of water damage must not cost a second call."""
        vision = _read("Un mur de cuisine tache par l'humidite.", is_document=False)
        _v, ocr_call = self._run(vision, _read("never used"))
        ocr_call.assert_not_called()

    def test_an_unanswered_marker_does_not_trigger_ocr(self) -> None:
        """A model that ignores the question must not cause a paid second call."""
        vision = _read("Un mur de cuisine tache.", is_document=None)
        _v, ocr_call = self._run(vision, _read("never used"))
        ocr_call.assert_not_called()

    def test_a_failing_ocr_keeps_the_vision_read(self) -> None:
        """No OCR chain, or a failure: the document is still read, just less
        precisely — never lost, never left empty."""
        vision = _read("Photo d'une facture.", is_document=True)
        self._run(vision, _read(None, reason="chain_not_configured"))

        cached = _sidecar_path(self._filed()).read_text(encoding="utf-8")
        self.assertIn("Photo d'une facture", cached)
        self.assertNotIn("Transcription OCR", cached)

    def test_the_read_becomes_reliable_once_ocr_confirmed(self) -> None:
        """`read_via` must flip to "ocr", so the analysis prompt stops treating the
        content as a fallible image description — it is now a transcription."""
        vision = _read("Photo d'une facture.", is_document=True)
        ocr = _read("FACTURE N 2026-0412")
        captured: dict[str, str] = {}

        def capture(text, **kwargs):
            captured["read_via"] = kwargs.get("read_via", "")
            return pipeline.analyze_content(text, **kwargs)

        with patch.object(pipeline, "analyze_content", side_effect=capture):
            self._run(vision, ocr)

        self.assertEqual(captured.get("read_via"), "ocr")


if __name__ == "__main__":
    unittest.main()
