# pyright: reportUnknownVariableType=false
"""Reading the date a filename carries, instead of deleting it.

Found on a real video named `VoxRefiner_..._2026-04-18.mp4`: the fiche came back
with `document_date: null` and the stored file was dated from its modification
time. The date was right there in the name.

It was never a video-specific fault — the mechanism did not exist for any file
type. The date cascade went EXIF (images only) → a date stated in the CONTENT →
mtime, and the prompt asks for the document's date "if clearly stated in the
content". A filename date was never consulted. The irony is that the code already
recognised one, in `_strip_leading_date`, purely in order to remove it.

It stays invisible on ordinary documents because an invoice states its date and a
photo carries EXIF. A video has neither: nothing is spoken that gives a day, and
there is no capture tag to read.

Two properties matter here. It must find the obvious forms, and it must refuse
everything ambiguous — a date silently wrong by a month is worse than none, since
it decides both the filename prefix and the folder a series is filed under.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from procrafiler.naming import date_from_filename
from procrafiler.pipeline import _resolve_document_date

TODAY = date(2026, 8, 2)


class ParsingTests(unittest.TestCase):
    def test_the_forms_people_and_cameras_actually_write(self) -> None:
        cases = {
            "VoxRefiner_52453434354-2026-04-18.mp4": date(2026, 4, 18),
            "2026-04-18_notes.txt": date(2026, 4, 18),
            "reunion_2026_04_18.pdf": date(2026, 4, 18),
            "IMG_20260418_101112.jpg": date(2026, 4, 18),
            "facture 18-04-2026.pdf": date(2026, 4, 18),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(date_from_filename(name, today=TODAY), expected)

    def test_an_ambiguous_day_month_order_is_refused(self) -> None:
        """03-04-2026 is 3 April to half the world and 4 March to the other half.
        Guessing would silently misfile by a month; only an unambiguous first
        group (13 or above, so it cannot be a month) is accepted."""
        self.assertIsNone(date_from_filename("note_03-04-2026.txt", today=TODAY))
        self.assertEqual(date_from_filename("note_18-04-2026.txt", today=TODAY), date(2026, 4, 18))

    def test_digits_that_are_not_a_date_are_refused(self) -> None:
        for name in (
            "2194i2e3ae754.webm",       # the id that started all this
            "releve_2026-13-45.pdf",    # month 13, day 45
            "invoice_99999999.pdf",
            "v1_2026.pdf",              # a bare year is not a date
            "ref_202604.pdf",           # month precision only
        ):
            with self.subTest(name=name):
                self.assertIsNone(date_from_filename(name, today=TODAY))

    def test_a_future_date_is_refused(self) -> None:
        """A version number or an id that happens to parse would otherwise sort
        above every real document the user owns, forever."""
        self.assertIsNone(date_from_filename("export_2099-12-31.csv", today=TODAY))
        self.assertIsNone(date_from_filename("v2_20991231.mp4", today=TODAY))

    def test_an_absurdly_old_date_is_refused(self) -> None:
        self.assertIsNone(date_from_filename("scan_1899-01-01.pdf", today=TODAY))

    def test_a_date_glued_to_more_digits_is_not_a_date(self) -> None:
        """`202604189` CONTAINS a valid `20260418`, and without the digit guards a
        scan of the string finds it — that is how an account reference or an order
        number becomes a filing date. The eight digits count only when nothing
        numeric touches them on either side."""
        self.assertIsNone(date_from_filename("ref_202604189.pdf", today=TODAY))
        self.assertIsNone(date_from_filename("ref_920260418.pdf", today=TODAY))
        self.assertEqual(date_from_filename("ref_20260418.pdf", today=TODAY), date(2026, 4, 18))


class CascadeTests(unittest.TestCase):
    """Where the filename date sits among the other sources."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _file(self, name: str, *, mtime: datetime) -> Path:
        path = self.dir / name
        path.write_bytes(b"x")
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        return path

    def test_a_date_in_the_content_still_wins(self) -> None:
        """The document saying its own date outranks anything written on the
        outside of it."""
        path = self._file("note_2020-01-01.pdf", mtime=datetime(2024, 1, 1, tzinfo=timezone.utc))
        resolved = _resolve_document_date("2026-04-18", path, self.now)
        self.assertEqual(resolved.date(), date(2026, 4, 18))

    def test_the_filename_beats_the_modification_time(self) -> None:
        """mtime says when the bytes were last touched — a copy, a download or a
        `chmod` rewrites it. Someone typing a date into a name meant it."""
        path = self._file("VoxRefiner_2026-04-18.mp4", mtime=datetime(2026, 7, 30, tzinfo=timezone.utc))
        resolved = _resolve_document_date(None, path, self.now)
        self.assertEqual(resolved.date(), date(2026, 4, 18))

    def test_without_a_filename_date_the_modification_time_still_applies(self) -> None:
        path = self._file("2194i2e3ae754.webm", mtime=datetime(2026, 5, 18, tzinfo=timezone.utc))
        resolved = _resolve_document_date(None, path, self.now)
        self.assertEqual(resolved.date(), date(2026, 5, 18))

    def test_the_resolved_date_is_midnight_utc(self) -> None:
        """A day, not an instant — so files of the same day group together instead
        of being scattered by the second they happened to be processed."""
        path = self._file("clip_2026-04-18.mp4", mtime=datetime(2026, 7, 30, tzinfo=timezone.utc))
        resolved = _resolve_document_date(None, path, self.now)
        self.assertEqual((resolved.hour, resolved.minute, resolved.second), (0, 0, 0))
        self.assertEqual(resolved.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
