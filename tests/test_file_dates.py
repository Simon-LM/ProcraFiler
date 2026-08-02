# pyright: reportUnknownVariableType=false
"""Reading the date a file carries in its own bytes — one format at a time.

Every one of these formats stores a creation date, and until now only photos had
theirs read. The tests build the real thing (a real EXIF block, a real PDF info
dictionary, a real office zip) rather than mocking an extractor, because what is
being pinned is that we speak each format correctly — a mock would pass against a
completely wrong key.

The other half of the contract is that failure is silent: a corrupt file, a
missing library, a format with no date at all must yield None, never an
exception. A file that will not open is a file with no embedded date, not a
reason to stop filing it.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from procrafiler.file_dates import (
    DateHint,
    date_evidence,
    embedded_date,
    format_date_evidence,
    modified_date,
)


def _jpeg_with_exif(path: Path, dt_str: str | None) -> Path:
    """A real JPEG, optionally carrying EXIF DateTimeOriginal (YYYY:MM:DD HH:MM:SS)."""
    image = Image.new("RGB", (4, 4), color="red")
    if dt_str is None:
        image.save(path)
        return path
    exif = image.getexif()
    exif[0x8769] = {36867: dt_str}  # Exif sub-IFD with DateTimeOriginal
    image.save(path, exif=exif)
    return path


def _pdf_with_creation_date(path: Path, raw: str | None) -> Path:
    """A real one-page PDF, optionally with /CreationDate in PDF date syntax."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if raw is not None:
        writer.add_metadata({"/CreationDate": raw})
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def _ooxml(path: Path, created: str | None) -> Path:
    """A minimal .docx: a zip whose docProps/core.xml holds dcterms:created."""
    body = (
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
        if created is not None
        else ""
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "docProps/core.xml",
            '<?xml version="1.0"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"{body}</cp:coreProperties>",
        )
    return path


def _odf(path: Path, created: str | None) -> Path:
    """A minimal .odt: a zip whose meta.xml holds meta:creation-date."""
    body = f"<meta:creation-date>{created}</meta:creation-date>" if created is not None else ""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "meta.xml",
            '<?xml version="1.0"?>'
            '<office:document-meta '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">'
            f"<office:meta>{body}</office:meta></office:document-meta>",
        )
    return path


class ExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_photo_gives_its_capture_date(self) -> None:
        hint = embedded_date(_jpeg_with_exif(self.dir / "p.jpg", "2025:08:01 20:39:18"), "image")
        assert hint is not None
        self.assertEqual(hint.source, "exif")
        self.assertEqual(hint.value, datetime(2025, 8, 1, 20, 39, 18, tzinfo=timezone.utc))

    def test_a_pdf_gives_its_production_date(self) -> None:
        """The date that was being thrown away: for a scan it is the only one the
        file has, and nothing read it before."""
        hint = embedded_date(_pdf_with_creation_date(self.dir / "d.pdf", "D:20260312093000+01'00'"), "pdf")
        assert hint is not None
        self.assertEqual(hint.source, "pdf")
        self.assertEqual(hint.value, datetime(2026, 3, 12, 9, 30, tzinfo=timezone(timedelta(hours=1))))

    def test_a_word_document_gives_its_creation_date(self) -> None:
        hint = embedded_date(_ooxml(self.dir / "d.docx", "2026-02-01T10:15:00Z"), "text")
        assert hint is not None
        self.assertEqual(hint.source, "ooxml")
        self.assertEqual(hint.value, datetime(2026, 2, 1, 10, 15, tzinfo=timezone.utc))

    def test_a_spreadsheet_gives_its_creation_date(self) -> None:
        """.xlsx is dispatched as "office" and .docx as "text" — two media types,
        one storage format. Keying the extractor on the media type would miss one."""
        hint = embedded_date(_ooxml(self.dir / "s.xlsx", "2026-02-01T10:15:00Z"), "office")
        assert hint is not None
        self.assertEqual(hint.source, "ooxml")

    def test_a_libreoffice_document_gives_its_creation_date(self) -> None:
        hint = embedded_date(_odf(self.dir / "d.odt", "2024-11-05T08:00:00"), "text")
        assert hint is not None
        self.assertEqual(hint.source, "odf")
        self.assertEqual(hint.value, datetime(2024, 11, 5, 8, 0, tzinfo=timezone.utc))

    def test_a_recording_gives_the_container_tag(self) -> None:
        made = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        video = self.dir / "v.mp4"
        video.write_bytes(b"not really a video")
        with patch("procrafiler.file_dates.container_creation_time", return_value=made) as probe:
            hint = embedded_date(video, "video")
        probe.assert_called_once()
        assert hint is not None
        self.assertEqual(hint.source, "container")
        self.assertEqual(hint.value, made)

    def test_an_audio_file_is_dated_like_a_video(self) -> None:
        """Audio is a first-class input, not a video without pictures."""
        song = self.dir / "a.mp3"
        song.write_bytes(b"x")
        with patch("procrafiler.file_dates.container_creation_time", return_value=datetime(2026, 1, 2)) as probe:
            hint = embedded_date(song, "audio")
        probe.assert_called_once()
        assert hint is not None
        self.assertEqual(hint.source, "container")

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Half these formats store no timezone. Returning a naive datetime would
        blow up the moment it is compared with the pipeline's aware ones."""
        song = self.dir / "a.mp3"
        song.write_bytes(b"x")
        with patch("procrafiler.file_dates.container_creation_time", return_value=datetime(2026, 1, 2, 3, 4)):
            hint = embedded_date(song, "audio")
        assert hint is not None
        self.assertIsNotNone(hint.value.tzinfo)


class NoDateTests(unittest.TestCase):
    """None is an ordinary answer; an exception is not."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_photo_without_exif(self) -> None:
        self.assertIsNone(embedded_date(_jpeg_with_exif(self.dir / "p.jpg", None), "image"))

    def test_a_pdf_without_a_creation_date(self) -> None:
        self.assertIsNone(embedded_date(_pdf_with_creation_date(self.dir / "d.pdf", None), "pdf"))

    def test_an_office_file_without_the_property(self) -> None:
        self.assertIsNone(embedded_date(_ooxml(self.dir / "d.docx", None), "text"))
        self.assertIsNone(embedded_date(_odf(self.dir / "d.odt", None), "text"))

    def test_an_unparseable_value_is_not_a_crash(self) -> None:
        self.assertIsNone(embedded_date(_ooxml(self.dir / "d.docx", "sometime last spring"), "text"))

    def test_a_corrupt_file_of_every_kind(self) -> None:
        for name in ("broken.pdf", "broken.docx", "broken.odt", "broken.jpg"):
            with self.subTest(name=name):
                path = self.dir / name
                path.write_bytes(b"\x00\x01 definitely not this format")
                self.assertIsNone(embedded_date(path, "pdf"))

    def test_a_missing_file(self) -> None:
        self.assertIsNone(embedded_date(self.dir / "gone.pdf", "pdf"))

    def test_a_plain_text_file_has_nowhere_to_store_one(self) -> None:
        txt = self.dir / "notes.txt"
        txt.write_bytes(b"hello")
        self.assertIsNone(embedded_date(txt, "text"))

    def test_an_unknown_extension_consults_nothing(self) -> None:
        odd = self.dir / "thing.xyz"
        odd.write_bytes(b"hello")
        self.assertIsNone(embedded_date(odd, None))

    def test_pillow_absent_only_disables_exif_dating(self) -> None:
        photo = _jpeg_with_exif(self.dir / "p.jpg", "2025:08:01 20:39:18")
        real_import = __import__

        def without_pillow(name: str, *args: object, **kwargs: object):
            if name == "PIL":
                raise ImportError("no Pillow")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        with patch("builtins.__import__", side_effect=without_pillow):
            self.assertIsNone(embedded_date(photo, "image"))


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_file_always_has_at_least_its_mtime(self) -> None:
        txt = self.dir / "notes.txt"
        txt.write_bytes(b"hello")
        hints = date_evidence(txt, "text")
        self.assertEqual([hint.source for hint in hints], ["mtime"])

    def test_a_photo_offers_both_its_exif_and_its_mtime(self) -> None:
        """Both are shown. Which one this document's date really is depends on
        whether it is a holiday snap or a photographed letter — a question only the
        model that read it can answer."""
        photo = _jpeg_with_exif(self.dir / "p.jpg", "2025:08:01 20:39:18")
        self.assertEqual([hint.source for hint in date_evidence(photo, "image")], ["exif", "mtime"])

    def test_a_missing_file_offers_nothing_and_does_not_raise(self) -> None:
        self.assertEqual(date_evidence(self.dir / "gone.txt", "text"), [])

    def test_the_mtime_is_the_real_one(self) -> None:
        txt = self.dir / "notes.txt"
        txt.write_bytes(b"hello")
        when = datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc)
        os.utime(txt, (when.timestamp(), when.timestamp()))
        hint = modified_date(txt)
        assert hint is not None
        self.assertEqual(hint.value, when)


class PromptBlockTests(unittest.TestCase):
    """What the model actually reads.

    The block must state what each timestamp ATTESTS, not which one to prefer:
    the whole point of collecting them here and deciding there is that the
    decision depends on the content.
    """

    def test_nothing_to_show_produces_nothing(self) -> None:
        self.assertEqual(format_date_evidence([]), "")

    def test_each_date_appears_with_what_it_means(self) -> None:
        block = format_date_evidence([
            DateHint("exif", datetime(2025, 8, 1, 20, 39, tzinfo=timezone.utc)),
            DateHint("mtime", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ])
        self.assertIn("2025-08-01", block)
        self.assertIn("2026-01-01", block)
        self.assertIn("camera", block.lower())
        self.assertIn("disk", block.lower())

    def test_a_pdf_date_is_described_as_the_files_own_production(self) -> None:
        """A scan's /CreationDate is the day it was scanned. Presenting it as "the
        document's date" would make the model date a 1998 letter to 2026."""
        block = format_date_evidence([DateHint("pdf", datetime(2026, 7, 1, tzinfo=timezone.utc))])
        self.assertIn("scan", block.lower())

    def test_it_does_not_tell_the_model_which_one_wins(self) -> None:
        block = format_date_evidence([
            DateHint("exif", datetime(2025, 8, 1, tzinfo=timezone.utc)),
            DateHint("pdf", datetime(2026, 7, 1, tzinfo=timezone.utc)),
            DateHint("mtime", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ])
        lowered = block.lower()
        for imperative in ("always use", "prefer the", "must use", "takes precedence", "overrides"):
            with self.subTest(imperative=imperative):
                self.assertNotIn(imperative, lowered)

    def test_it_says_these_date_the_file_not_necessarily_the_document(self) -> None:
        block = format_date_evidence([DateHint("mtime", datetime(2026, 1, 1, tzinfo=timezone.utc))])
        self.assertIn("file", block.lower())
        self.assertIn("document", block.lower())


if __name__ == "__main__":
    unittest.main()
