# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.ai_analysis import _extract_document_date
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.file_dates import DateHint
from procrafiler.pipeline import _resolve_document_date, process_next_inbox_file


class TestExtractDocumentDate(unittest.TestCase):
    # _extract_document_date now receives the already-parsed analysis payload.
    def test_valid_date(self) -> None:
        self.assertEqual(_extract_document_date({"name": "x", "date": "2026-04-30"}), "2026-04-30")

    def test_null_date(self) -> None:
        self.assertIsNone(_extract_document_date({"name": "x", "date": None}))

    def test_missing_date(self) -> None:
        self.assertIsNone(_extract_document_date({"name": "x"}))

    def test_bad_format(self) -> None:
        self.assertIsNone(_extract_document_date({"name": "x", "date": "30/04/2026"}))

    def test_impossible_date(self) -> None:
        self.assertIsNone(_extract_document_date({"name": "x", "date": "2026-13-45"}))


class TestResolveDocumentDate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.f = Path(self.tmp.name) / "doc.txt"
        self.f.write_bytes(b"x")
        self.now = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ai_date_wins_and_is_midnight(self) -> None:
        dt = _resolve_document_date("2026-04-30", self.f, self.now)
        self.assertEqual(dt, datetime(2026, 4, 30, 0, 0, 0, tzinfo=timezone.utc))

    def test_falls_back_to_mtime(self) -> None:
        mtime = datetime(2026, 3, 15, 14, 30, 0, tzinfo=timezone.utc).timestamp()
        os.utime(self.f, (mtime, mtime))
        dt = _resolve_document_date(None, self.f, self.now)
        self.assertEqual(dt, datetime(2026, 3, 15, 14, 30, 0, tzinfo=timezone.utc))

    def test_invalid_ai_date_falls_back_to_mtime(self) -> None:
        mtime = datetime(2026, 3, 15, 14, 30, 0, tzinfo=timezone.utc).timestamp()
        os.utime(self.f, (mtime, mtime))
        dt = _resolve_document_date("pas-une-date", self.f, self.now)
        self.assertEqual(dt, datetime(2026, 3, 15, 14, 30, 0, tzinfo=timezone.utc))

    def test_missing_file_falls_back_to_now(self) -> None:
        dt = _resolve_document_date(None, Path(self.tmp.name) / "nope.txt", self.now)
        self.assertEqual(dt, self.now)


class TestEmbeddedFallback(unittest.TestCase):
    """What happens when the analysis established no date at all.

    Steps 2-4 are a fallback ladder, not a ranking of evidence: the ranking was
    already done, by the model that saw the content AND these timestamps. Here
    there is no judgement to honour, and every file must still end up dated.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone.utc)
        self.f = self.dir / "doc.pdf"
        self.f.write_bytes(b"x")
        mtime = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        os.utime(self.f, (mtime, mtime))
        self.embedded = DateHint("pdf", datetime(2025, 8, 1, 20, 39, 18, tzinfo=timezone.utc))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_analysis_date_is_honoured_over_the_file_metadata(self) -> None:
        """The regression this whole re-architecture exists to prevent.

        A scanned 1998 letter has a 2026 /CreationDate and a photographed invoice
        has today's EXIF. The metadata used to win in code, so those documents were
        filed under the day they were digitised. The model is shown both and says
        which is the DOCUMENT's date; that answer stands.
        """
        dt = _resolve_document_date("1998-03-04", self.f, self.now, embedded=self.embedded)
        self.assertEqual(dt, datetime(1998, 3, 4, 0, 0, 0, tzinfo=timezone.utc))

    def test_no_analysis_date_falls_back_to_the_file_metadata(self) -> None:
        dt = _resolve_document_date(None, self.f, self.now, embedded=self.embedded)
        self.assertEqual(dt, self.embedded.value)

    def test_the_metadata_beats_the_mtime(self) -> None:
        """A download rewrites the mtime and leaves the embedded date alone, so the
        embedded one is the better guess when nothing better exists."""
        dt = _resolve_document_date(None, self.f, self.now, embedded=self.embedded)
        self.assertNotEqual(dt, datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

    def test_an_unparseable_analysis_date_still_falls_back(self) -> None:
        dt = _resolve_document_date("pas-une-date", self.f, self.now, embedded=self.embedded)
        self.assertEqual(dt, self.embedded.value)

    def test_no_metadata_falls_through_to_the_mtime(self) -> None:
        dt = _resolve_document_date(None, self.f, self.now, embedded=None)
        self.assertEqual(dt, datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc))

    def test_a_file_with_nothing_at_all_is_still_dated(self) -> None:
        """No content date, no metadata, no file to stat. Today is a worse answer
        than a real date and a better one than no name at all."""
        dt = _resolve_document_date(None, self.dir / "gone.bin", self.now, embedded=None)
        self.assertEqual(dt, self.now)

    def test_the_ladder_is_the_same_for_every_media_type(self) -> None:
        """A photo, a video and a PDF go through one cascade. Only the extractor
        that produced `embedded` differed, and that happened earlier."""
        for name in ("p.jpg", "v.mp4", "d.pdf", "s.xlsx", "notes.txt"):
            with self.subTest(name=name):
                path = self.dir / name
                path.write_bytes(b"x")
                self.assertEqual(
                    _resolve_document_date("2026-04-30", path, self.now, embedded=self.embedded),
                    datetime(2026, 4, 30, 0, 0, 0, tzinfo=timezone.utc),
                )


class TestDocumentDatePipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for k in ("PROCRAFILER_AI_ANALYSIS_PRIMARY",):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_the_files_own_timestamps_reach_the_analysis_prompt(self) -> None:
        """The wiring, end to end: what `file_dates` collected must actually be in
        the text the model reads. Everything else here is a rule the model can only
        follow if it was told."""
        scan = self.paths.inbox_dir / "scan.txt"
        scan.write_bytes(b"Facture du 30 avril 2026, montant 84 EUR")
        when = datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc).timestamp()
        os.utime(scan, (when, when))

        seen: list[str] = []

        def capture(prompt: str, *args: object, **kwargs: object) -> str:
            seen.append(prompt)
            return '{"name":"Facture EDF","category_path":"Personal/Administrative","date":"2026-04-30"}'

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=capture):
            process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertTrue(seen, "the analysis was never called")
        self.assertIn("2026-03-15", seen[0])
        self.assertIn("written to disk", seen[0])

    def test_the_analysis_date_wins_over_the_files_timestamp(self) -> None:
        """The mtime says March because that is when the file was downloaded; the
        invoice says 30 April. The model saw both and chose — and its answer is what
        dates the file."""
        scan = self.paths.inbox_dir / "scan.txt"
        scan.write_bytes(b"Facture du 30 avril 2026, montant 84 EUR")
        when = datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc).timestamp()
        os.utime(scan, (when, when))

        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value='{"name":"Facture EDF","category_path":"Personal/Administrative","date":"2026-04-30"}',
        ):
            process_next_inbox_file(self.paths, now_utc=self.now)

        files = [p for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("2026-04-30_"), files[0].name)

    def test_no_analysis_date_leaves_the_file_dated_by_its_own_metadata(self) -> None:
        """A file nothing could date must still be filed, not left undated."""
        scan = self.paths.inbox_dir / "scan.txt"
        scan.write_bytes(b"quelques notes sans aucune date")
        when = datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc).timestamp()
        os.utime(scan, (when, when))

        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value='{"name":"Notes","category_path":"Personal/Misc","date":null}',
        ):
            process_next_inbox_file(self.paths, now_utc=self.now)

        files = [p for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("2026-03-15_"), files[0].name)

    def test_filename_uses_ai_document_date_at_midnight(self) -> None:
        (self.paths.inbox_dir / "scan.txt").write_bytes(b"Facture du 30 avril 2026, montant 84 EUR")

        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value='{"name":"Facture EDF","category_path":"Personal/Administrative","date":"2026-04-30"}',
        ):
            status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        files = [p for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)
        # Dated by the content (30 April), at midnight — not the processing day.
        self.assertTrue(files[0].name.startswith("2026-04-30_00-00-00__"))
        self.assertIn("Facture-EDF", files[0].name)


if __name__ == "__main__":
    unittest.main()
