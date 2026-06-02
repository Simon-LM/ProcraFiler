# pyright: reportUnknownVariableType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_next_inbox_file


class TestOcrPipeline(unittest.TestCase):
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
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for key in (
            "PROCRAFILER_AI_OCR_PRIMARY",
            "PROCRAFILER_AI_IMAGE_PRIMARY",
            "PROCRAFILER_AI_ANALYSIS_PRIMARY",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _events(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_scanned_pdf_is_ocr_read_then_classified(self) -> None:
        os.environ["PROCRAFILER_AI_OCR_PRIMARY"] = "mistral:mistral-ocr-latest"
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        # Fake PDF bytes: pypdf can't extract a text layer -> reader_hint "ocr".
        (self.paths.inbox_dir / "scan.pdf").write_bytes(b"%PDF-1.4 scanned, no text layer")

        with patch("procrafiler.ai_reader.call_mistral_ocr", return_value="Releve de compte BNP avril 2026"):
            with patch(
                "procrafiler.ai_analysis.call_mistral_chat",
                return_value='{"name": "Releve BNP", "category_path": "Banque"}',
            ):
                status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        banque_files = [p for p in (self.paths.library_root / "Banque").rglob("*") if p.is_file()]
        self.assertEqual(len(banque_files), 1)

        events = self._events()
        self.assertTrue(any(e["action"] == "ocr_read_success" for e in events))
        self.assertTrue(any(e["action"] == "analysis_success" and e["category"] == "Banque" for e in events))

    def test_scanned_pdf_without_ocr_chain_goes_to_manual_review(self) -> None:
        # No OCR chain configured -> OCR unavailable -> Revue_Manuelle.
        (self.paths.inbox_dir / "scan.pdf").write_bytes(b"%PDF-1.4 scanned, no text layer")
        status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        review_files = [p for p in (self.paths.library_root / "Revue_Manuelle").rglob("*") if p.is_file()]
        self.assertEqual(len(review_files), 1)
        events = self._events()
        self.assertTrue(any(e["action"] == "ocr_read_unavailable" for e in events))

    def test_image_is_vision_read_then_classified(self) -> None:
        os.environ["PROCRAFILER_AI_IMAGE_PRIMARY"] = "mistral:mistral-medium-latest"
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        (self.paths.inbox_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff fake image bytes")

        with patch("procrafiler.ai_reader.call_mistral_vision", return_value="Recu de carte bancaire, total 42 EUR"):
            with patch(
                "procrafiler.ai_analysis.call_mistral_chat",
                return_value='{"name": "Recu carte", "category_path": "Banque"}',
            ):
                status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        banque_files = [p for p in (self.paths.library_root / "Banque").rglob("*") if p.is_file()]
        self.assertEqual(len(banque_files), 1)
        events = self._events()
        self.assertTrue(any(e["action"] == "vision_read_success" for e in events))

    def test_image_without_vision_chain_goes_to_manual_review(self) -> None:
        (self.paths.inbox_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff fake image bytes")
        status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        review_files = [p for p in (self.paths.library_root / "Revue_Manuelle").rglob("*") if p.is_file()]
        self.assertEqual(len(review_files), 1)
        events = self._events()
        self.assertTrue(any(e["action"] == "vision_read_unavailable" for e in events))


if __name__ == "__main__":
    unittest.main()
