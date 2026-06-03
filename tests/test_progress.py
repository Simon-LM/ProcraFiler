# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files


class TestProgress(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_AI_")]:
            os.environ.pop(k, None)
        os.environ.pop("MISTRAL_API_KEY", None)
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        self.tmp.cleanup()

    def test_progress_lines_for_manual_review(self) -> None:
        (self.paths.inbox_dir / "note.txt").write_bytes(b"contenu lisible")
        lines: list[str] = []
        process_all_inbox_files(self.paths, now_utc=self.now, progress=lines.append)
        joined = "\n".join(lines)
        self.assertIn("note.txt", joined)         # the "→ <file>" line
        self.assertIn("read: text", joined)        # local extraction
        self.assertIn("manual review", joined.lower())

    def test_progress_lines_for_classified_file(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        (self.paths.inbox_dir / "rel.txt").write_bytes(b"Releve de compte bancaire")
        lines: list[str] = []
        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value='{"name": "Releve", "category_path": "Personal/Administrative/Banking"}',
        ):
            process_all_inbox_files(self.paths, now_utc=self.now, progress=lines.append)
        joined = "\n".join(lines)
        self.assertIn("classified → Personal/Administrative/Banking", joined)
        self.assertIn("filed → Personal/Administrative/Banking/", joined)

    def test_no_callback_is_safe(self) -> None:
        (self.paths.inbox_dir / "x.txt").write_bytes(b"x")
        summary = process_all_inbox_files(self.paths, now_utc=self.now)  # progress=None
        self.assertEqual(summary["processed"], 1)


if __name__ == "__main__":
    unittest.main()
