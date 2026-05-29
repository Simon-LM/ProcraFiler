# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf.errors import PyPdfError

from procrafiler.content_reader import (
    READER_HINT_OCR,
    READER_HINT_VISION,
    extract_text_content,
)


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdfReader:
    def __init__(self, pages_text: list[str]) -> None:
        self.pages = [_FakePage(t) for t in pages_text]


class TestContentReader(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        p = self.root / name
        p.write_bytes(data)
        return p

    # --- text files ---------------------------------------------------------

    def test_text_file_with_content(self) -> None:
        path = self._write("note.txt", "Facture EDF avril 2026".encode("utf-8"))
        result = extract_text_content(path, "text")
        self.assertEqual(result.reason, "text_extracted")
        self.assertFalse(result.needs_ai_reader)
        self.assertIn("Facture EDF", result.text or "")

    def test_empty_text_file(self) -> None:
        path = self._write("empty.txt", b"   \n  ")
        result = extract_text_content(path, "text")
        self.assertEqual(result.reason, "empty")
        self.assertFalse(result.needs_ai_reader)

    # --- PDFs (pypdf mocked so we test OUR logic, not pypdf) ----------------

    def test_readable_pdf_extracts_text(self) -> None:
        path = self._write("doc.pdf", b"%PDF-fake")
        fake = _FakePdfReader(["This is a long enough readable text layer."])
        with patch("procrafiler.content_reader.PdfReader", return_value=fake):
            result = extract_text_content(path, "pdf")
        self.assertEqual(result.reason, "text_extracted")
        self.assertFalse(result.needs_ai_reader)
        self.assertIn("readable text layer", result.text or "")

    def test_scanned_pdf_needs_ocr(self) -> None:
        path = self._write("scan.pdf", b"%PDF-fake")
        fake = _FakePdfReader(["", ""])  # no text layer
        with patch("procrafiler.content_reader.PdfReader", return_value=fake):
            result = extract_text_content(path, "pdf")
        self.assertEqual(result.reason, "scanned_pdf_needs_ocr")
        self.assertTrue(result.needs_ai_reader)
        self.assertEqual(result.reader_hint, READER_HINT_OCR)
        self.assertIsNone(result.text)

    def test_pdf_below_threshold_treated_as_scanned(self) -> None:
        path = self._write("tiny.pdf", b"%PDF-fake")
        fake = _FakePdfReader(["ab"])  # a few stray chars, under the threshold
        with patch("procrafiler.content_reader.PdfReader", return_value=fake):
            result = extract_text_content(path, "pdf")
        self.assertEqual(result.reason, "scanned_pdf_needs_ocr")
        self.assertTrue(result.needs_ai_reader)

    def test_corrupt_pdf_falls_back_to_ocr(self) -> None:
        path = self._write("broken.pdf", b"not a pdf at all")
        with patch("procrafiler.content_reader.PdfReader", side_effect=PyPdfError("boom")):
            result = extract_text_content(path, "pdf")
        self.assertEqual(result.reason, "pdf_extract_error")
        self.assertTrue(result.needs_ai_reader)
        self.assertEqual(result.reader_hint, READER_HINT_OCR)

    # --- images and unsupported --------------------------------------------

    def test_image_needs_vision(self) -> None:
        path = self._write("photo.jpg", b"\xff\xd8\xff")
        result = extract_text_content(path, "image")
        self.assertEqual(result.reason, "image_needs_vision")
        self.assertTrue(result.needs_ai_reader)
        self.assertEqual(result.reader_hint, READER_HINT_VISION)

    def test_unsupported_media_type(self) -> None:
        path = self._write("archive.zip", b"PK\x03\x04")
        result = extract_text_content(path, "archive")
        self.assertEqual(result.reason, "unsupported_local_extraction")
        self.assertFalse(result.needs_ai_reader)
        self.assertIsNone(result.reader_hint)


if __name__ == "__main__":
    unittest.main()
