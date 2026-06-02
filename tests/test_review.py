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
from procrafiler.pipeline import (
    PendingDecisionError,
    process_next_inbox_file,
    resolve_pending_decision,
    run_review,
)
from procrafiler.taxonomy import normalize_review_path


class TestNormalizeReviewPath(unittest.TestCase):
    def test_existing_base_kept(self) -> None:
        self.assertEqual(normalize_review_path("Banque", 10), ("Banque",))

    def test_subfolder_under_existing_base(self) -> None:
        self.assertEqual(normalize_review_path("Administratif/Impots", 10), ("Administratif", "Impots"))

    def test_new_root_is_allowed_here(self) -> None:
        # Unlike the AI path (normalize_category_path), review may create a new root.
        self.assertEqual(normalize_review_path("Sante/Ordonnances", 10), ("Sante", "Ordonnances"))

    def test_case_insensitive_snaps_onto_existing_base(self) -> None:
        # Typing "banque" must not fork a lowercase duplicate of "Banque".
        self.assertEqual(normalize_review_path("banque/releves", 10), ("Banque", "releves"))

    def test_segments_are_slugified(self) -> None:
        self.assertEqual(normalize_review_path("Santé/Ordonnances 2026", 10), ("Sante", "Ordonnances-2026"))

    def test_empty_is_rejected(self) -> None:
        self.assertIsNone(normalize_review_path("   ", 10))

    def test_depth_cap_truncates_new_root(self) -> None:
        self.assertEqual(normalize_review_path("Sante/a/b/c", 2), ("Sante", "a"))


class _PipelineTestCase(unittest.TestCase):
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
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        self.tmp.cleanup()

    def _process(self, ai_output: dict[str, object], content: bytes = b"contenu") -> str:
        (self.paths.inbox_dir / "lettre.txt").write_bytes(content)
        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value=json.dumps(ai_output),
        ):
            return process_next_inbox_file(self.paths, now_utc=self.now)

    def _repo(self) -> CatalogRepository:
        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        return repo

    def _files_under(self, *parts: str) -> list[Path]:
        base = self.paths.library_root.joinpath(*parts)
        return [p for p in base.rglob("*") if p.is_file()] if base.exists() else []

    def _mirror_files(self) -> list[Path]:
        return [p for p in self.paths.mirror_root.rglob("*") if p.is_file()]


class TestParking(_PipelineTestCase):
    def test_uncertain_with_options_is_parked(self) -> None:
        state = self._process({"category_path": None, "alternatives": ["Banque", "Administratif/Impots"]})
        # Physically stored (flow state) but parked for review (catalog status).
        self.assertEqual(state, "LIBRARY_STORED")
        self.assertEqual(len(self._files_under("Revue_Manuelle")), 1)

        pending = self._repo().list_pending_decisions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "DECISION_PENDING")
        blob = json.loads(pending[0]["pending_decision"])
        self.assertEqual(blob["options"], ["Banque", "Administratif/Impots"])

        # A parked file is NOT mirrored: its placement is not settled yet.
        self.assertEqual(self._mirror_files(), [])

    def test_invalid_options_fall_back_to_plain_manual_review(self) -> None:
        # Options that don't map to any base survive validation as none → this is
        # ordinary manual review (settled in Revue_Manuelle, and mirrored).
        state = self._process({"category_path": None, "alternatives": ["Loisirs/Audio", "Inexistant"]})
        self.assertEqual(state, "LIBRARY_STORED")
        self.assertEqual(self._repo().list_pending_decisions(), [])
        self.assertEqual(len(self._files_under("Revue_Manuelle")), 1)
        self.assertEqual(len(self._mirror_files()), 1)  # settled → mirrored

    def test_confident_path_is_not_parked(self) -> None:
        state = self._process({"category_path": "Banque", "alternatives": ["Administratif"]})
        self.assertEqual(state, "LIBRARY_STORED")
        self.assertEqual(self._repo().list_pending_decisions(), [])
        self.assertEqual(len(self._files_under("Banque")), 1)
        self.assertEqual(len(self._mirror_files()), 1)


class TestRunReview(_PipelineTestCase):
    def _park_one(self) -> None:
        self._process({"category_path": None, "alternatives": ["Banque", "Administratif/Impots"]})

    def _review(self, answers: list[str]) -> tuple[dict[str, int], list[str]]:
        out: list[str] = []
        it = iter(answers)
        summary = run_review(
            self.paths,
            input_fn=lambda _prompt: next(it),
            output_fn=out.append,
            now_utc=self.now,
        )
        return summary, out

    def test_no_pending_reports_nothing(self) -> None:
        summary, out = self._review([])
        self.assertEqual(summary, {"pending": 0, "resolved": 0, "skipped": 0})
        self.assertIn("No decisions pending.", out)

    def test_resolve_by_picking_an_option(self) -> None:
        self._park_one()
        summary, _ = self._review(["1"])  # pick "Banque"
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(len(self._files_under("Banque")), 1)
        self.assertEqual(self._files_under("Revue_Manuelle"), [])
        # Settled now → mirrored, and the queue is empty.
        self.assertEqual(len(self._mirror_files()), 1)
        self.assertEqual(self._repo().list_pending_decisions(), [])
        # The fiche is kept and its category_path now reflects the user's choice.
        doc = self._repo().list_documents()[0]
        self.assertEqual(json.loads(doc["content_json"])["category_path"], "Banque")

    def test_resolve_with_custom_new_root(self) -> None:
        self._park_one()
        summary, _ = self._review(["Sante/Ordonnances"])
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(len(self._files_under("Sante", "Ordonnances")), 1)
        self.assertEqual(self._repo().list_pending_decisions(), [])

    def test_skip_keeps_it_pending(self) -> None:
        self._park_one()
        summary, _ = self._review(["s"])
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["resolved"], 0)
        self.assertEqual(len(self._files_under("Revue_Manuelle")), 1)
        self.assertEqual(len(self._repo().list_pending_decisions()), 1)


class TestResolveDirect(_PipelineTestCase):
    def test_invalid_label_raises(self) -> None:
        self._process({"category_path": None, "alternatives": ["Banque"]})
        record = self._repo().list_pending_decisions()[0]
        with self.assertRaises(PendingDecisionError):
            resolve_pending_decision(self.paths, record, "   ", now_utc=self.now)
        # Still parked, untouched.
        self.assertEqual(len(self._repo().list_pending_decisions()), 1)

    def test_missing_source_raises(self) -> None:
        self._process({"category_path": None, "alternatives": ["Banque"]})
        record = dict(self._repo().list_pending_decisions()[0])
        record["current_path"] = str(self.paths.library_root / "Revue_Manuelle" / "gone.txt")
        with self.assertRaises(PendingDecisionError):
            resolve_pending_decision(self.paths, record, "Banque", now_utc=self.now)


if __name__ == "__main__":
    unittest.main()
