from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import types

from procrafiler.pipeline import (
    _mirror_text_sidecar,
    _move_text_sidecar,
    _sidecar_path,
    _write_text_sidecar,
)


class TestTextSidecars(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sidecar_path_is_hidden_next_to_the_file(self) -> None:
        self.assertEqual(
            _sidecar_path(self.root / "X.pdf"),
            self.root / ".X.pdf.txt",
        )

    def test_written_only_for_ocr_and_vision(self) -> None:
        doc = self.root / "scan.pdf"
        doc.write_bytes(b"%PDF")
        # text / readable PDF / unreadable → no sidecar (re-derivable or none)
        for read_via in ("text", None):
            _write_text_sidecar(doc, read_via, "some text")
            self.assertFalse(_sidecar_path(doc).exists(), read_via)
        # OCR / vision → the costly extracted text is kept
        _write_text_sidecar(doc, "ocr", "transcribed text")
        self.assertEqual(_sidecar_path(doc).read_text(encoding="utf-8"), "transcribed text")
        _write_text_sidecar(self.root / "img.png", "vision", "a described image")
        self.assertTrue((self.root / ".img.png.txt").is_file())

    def test_empty_text_writes_nothing(self) -> None:
        doc = self.root / "img.png"
        _write_text_sidecar(doc, "vision", "   ")
        self.assertFalse(_sidecar_path(doc).exists())

    def test_move_follows_the_document(self) -> None:
        old = self.root / "A" / "scan.pdf"
        old.parent.mkdir(parents=True)
        _write_text_sidecar(old, "ocr", "the text")
        new = self.root / "B" / "scan-renamed.pdf"
        _move_text_sidecar(old, new)
        self.assertFalse(_sidecar_path(old).exists())
        self.assertEqual(_sidecar_path(new).read_text(encoding="utf-8"), "the text")

    def test_move_is_a_noop_without_a_sidecar(self) -> None:
        old = self.root / "plain.txt"
        old.write_text("x", encoding="utf-8")
        _move_text_sidecar(old, self.root / "moved.txt")  # no sidecar → nothing happens
        self.assertFalse((self.root / ".moved.txt.txt").exists())

    def test_mirror_backs_up_the_sidecar(self) -> None:
        # The costly OCR/vision text must also live in the mirror, so it survives
        # if the primary library is lost.
        lib = self.root / "Library"
        mir = self.root / "Mirror"
        doc = lib / "Personal" / "scan.pdf"
        doc.parent.mkdir(parents=True)
        _write_text_sidecar(doc, "ocr", "the costly text")
        paths = types.SimpleNamespace(library_root=lib, mirror_root=mir)
        _mirror_text_sidecar(paths, doc)
        self.assertEqual(
            (mir / "Personal" / ".scan.pdf.txt").read_text(encoding="utf-8"),
            "the costly text",
        )

    def test_mirror_sidecar_is_a_noop_without_a_sidecar(self) -> None:
        lib = self.root / "Library"
        mir = self.root / "Mirror"
        doc = lib / "Personal" / "plain.txt"
        doc.parent.mkdir(parents=True)
        doc.write_text("x", encoding="utf-8")  # readable → no sidecar
        paths = types.SimpleNamespace(library_root=lib, mirror_root=mir)
        _mirror_text_sidecar(paths, doc)  # no-op
        self.assertFalse((mir / "Personal" / ".plain.txt.txt").exists())


if __name__ == "__main__":
    unittest.main()
