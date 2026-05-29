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


class TestClassificationPipeline(unittest.TestCase):
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
        os.environ.pop("PROCRAFILER_AI_CLASSIFICATION_PRIMARY", None)
        self.tmp.cleanup()

    def _events(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_text_file_classified_into_real_category(self) -> None:
        os.environ["PROCRAFILER_AI_CLASSIFICATION_PRIMARY"] = "mistral:mistral-small-latest"
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"Releve de compte bancaire BNP avril 2026")

        with patch(
            "procrafiler.ai_classification.call_mistral_chat",
            return_value='{"category": "Banque"}',
        ):
            status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        # The file landed in the real AI-decided category, not Revue_Manuelle.
        banque_files = [p for p in (self.paths.library_root / "Banque").rglob("*") if p.is_file()]
        self.assertEqual(len(banque_files), 1)
        self.assertEqual(list((self.paths.library_root / "Revue_Manuelle").iterdir()), [])

        events = self._events()
        self.assertTrue(any(e["action"] == "classification_success" and e["category"] == "Banque" for e in events))

    def test_text_file_without_chain_goes_to_manual_review(self) -> None:
        # No CLASSIFICATION chain configured -> fallback -> Revue_Manuelle.
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"some readable content")
        status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        review_files = [p for p in (self.paths.library_root / "Revue_Manuelle").rglob("*") if p.is_file()]
        self.assertEqual(len(review_files), 1)
        events = self._events()
        self.assertTrue(any(e["action"] == "classification_manual_review" for e in events))

    def test_uncertain_ai_goes_to_manual_review(self) -> None:
        os.environ["PROCRAFILER_AI_CLASSIFICATION_PRIMARY"] = "mistral:mistral-small-latest"
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"ambiguous content")

        with patch(
            "procrafiler.ai_classification.call_mistral_chat",
            return_value='{"category": null}',
        ):
            status = process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(status, "LIBRARY_STORED")
        review_files = [p for p in (self.paths.library_root / "Revue_Manuelle").rglob("*") if p.is_file()]
        self.assertEqual(len(review_files), 1)


if __name__ == "__main__":
    unittest.main()
