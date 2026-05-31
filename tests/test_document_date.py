# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.ai_naming import _extract_document_date
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _resolve_document_date, process_next_inbox_file


class TestExtractDocumentDate(unittest.TestCase):
    def test_valid_date(self) -> None:
        self.assertEqual(_extract_document_date('{"stem":"x","date":"2026-04-30"}'), "2026-04-30")

    def test_null_date(self) -> None:
        self.assertIsNone(_extract_document_date('{"stem":"x","date":null}'))

    def test_missing_date(self) -> None:
        self.assertIsNone(_extract_document_date('{"stem":"x"}'))

    def test_bad_format(self) -> None:
        self.assertIsNone(_extract_document_date('{"stem":"x","date":"30/04/2026"}'))

    def test_impossible_date(self) -> None:
        self.assertIsNone(_extract_document_date('{"stem":"x","date":"2026-13-45"}'))


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


class TestDocumentDatePipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_NAMING_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for k in ("PROCRAFILER_AI_NAMING_PRIMARY",):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_filename_uses_ai_document_date_at_midnight(self) -> None:
        (self.paths.inbox_dir / "scan.txt").write_bytes(b"Facture du 30 avril 2026, montant 84 EUR")

        with patch(
            "procrafiler.ai_naming.call_mistral_chat",
            return_value='{"stem":"Facture EDF","date":"2026-04-30"}',
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
