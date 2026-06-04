# pyright: reportUnknownVariableType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files

AFFAIR = "Personal/Administrative/Insurance/Degats-eaux-2025-08"


class TestOrganizePipeline(unittest.TestCase):
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
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for key in ("PROCRAFILER_AI_ANALYSIS_PRIMARY", "PROCRAFILER_AI_ORGANIZE_PRIMARY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _drop_claim_folder(self) -> None:
        sub = self.paths.inbox_dir / "Dégats_eaux"
        sub.mkdir()
        (sub / "constat.txt").write_bytes(b"constat amiable de degat des eaux")
        (sub / "photo.txt").write_bytes(b"description d'une photo de moisissure")

    def _run(self) -> dict[str, int]:
        # Per-file analysis sends both to Insurance; the organize pass groups them.
        analysis = json.dumps({"name": "Doc", "category_path": "Personal/Administrative/Insurance", "summary": "sinistre"})
        organize = json.dumps({"placements": [{"index": 0, "path": AFFAIR}, {"index": 1, "path": AFFAIR}]})
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            with patch("procrafiler.ai_organize.call_mistral_chat", return_value=organize):
                return process_all_inbox_files(self.paths, now_utc=self.now)

    def _files_under(self, *parts: str) -> list[Path]:
        base = self.paths.library_root.joinpath(*parts)
        return [p for p in base.rglob("*") if p.is_file()] if base.exists() else []

    def test_set_is_grouped_into_a_dated_affair_folder(self) -> None:
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self._drop_claim_folder()
        summary = self._run()

        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["organized"], 2)
        # Both files end up grouped in the dated affair folder, not bare in Insurance/.
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Insurance", "Degats-eaux-2025-08")), 2)
        directly_in_insurance = [
            p for p in (self.paths.library_root / "Personal" / "Administrative" / "Insurance").iterdir() if p.is_file()
        ]
        self.assertEqual(directly_in_insurance, [])

    def test_catalog_and_mirror_follow_the_grouping(self) -> None:
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self._drop_claim_folder()
        self._run()

        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        for doc in repo.list_documents():
            self.assertIn("Insurance/Degats-eaux-2025-08", doc["current_path"])
            self.assertEqual(json.loads(doc["content_json"])["category_path"], AFFAIR)
        # The mirror copies moved with them.
        mirror_affair = [
            p for p in (self.paths.mirror_root / "Personal" / "Administrative" / "Insurance" / "Degats-eaux-2025-08").rglob("*") if p.is_file()
        ]
        self.assertEqual(len(mirror_affair), 2)

    def test_no_organize_chain_means_no_grouping(self) -> None:
        # Without an ORGANIZE chain, the second pass is a no-op: files stay where
        # the per-file analysis put them (bare Insurance/).
        self._drop_claim_folder()
        summary = self._run()
        self.assertEqual(summary["organized"], 0)
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Insurance")), 2)
        self.assertFalse((self.paths.library_root / "Personal" / "Administrative" / "Insurance" / "Degats-eaux-2025-08").exists())


if __name__ == "__main__":
    unittest.main()
