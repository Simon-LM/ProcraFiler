# pyright: reportUnknownVariableType=false
"""Set-aware NAMING: a file's name re-judged in the light of the folder it came in.

The case that motivates it: ten photos of a water-damage claim, one of which the
vision model describes as "a cat on a sofa". Analysed alone, that photo keeps an
absurd name; seen next to its nine neighbours, it is obviously part of the claim.

These tests assert on the built prompt and on the pipeline wiring — offline, with
the AI mocked. They can prove the whole set reaches the model and that its verdict
is applied; they cannot prove the model judges well. That needs a real run.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.ai_set_naming import _build_set_naming_prompt, name_set
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files

DOCS = [
    {"read_via": "vision", "original_filename": "IMG_001.jpg", "proposed_name": "Degats-eaux_cuisine", "summary": "Mur taché"},
    {"read_via": "vision", "original_filename": "IMG_002.jpg", "proposed_name": "Chat-sur-canape", "summary": "Un chat"},
    {"read_via": "text", "original_filename": "devis.pdf", "proposed_name": "Devis_Plombier-Martin", "summary": "Devis"},
]


class TestSetNamingPrompt(unittest.TestCase):
    def test_the_whole_set_is_shown_at_once(self) -> None:
        prompt = _build_set_naming_prompt(DOCS, "Degats-eaux", None)
        for doc in DOCS:
            self.assertIn(doc["original_filename"], prompt)
            self.assertIn(doc["proposed_name"], prompt)
        self.assertIn("Degats-eaux", prompt)

    def test_how_each_file_was_read_is_declared(self) -> None:
        """The pass must know which readings are weak — that is the whole point."""
        prompt = _build_set_naming_prompt(DOCS, "Degats-eaux", None)
        self.assertIn("read_as: vision", prompt)
        self.assertIn("read_as: text", prompt)

    def test_ocr_is_declared_reliable_and_image_description_weak(self) -> None:
        prompt = _build_set_naming_prompt(DOCS, "Degats-eaux", None)
        self.assertIn("OCR transcription of a document is reliable", prompt)
        self.assertIn("IMAGE DESCRIPTION", prompt)

    def test_the_prompt_leans_on_context_without_forcing_one_subject(self) -> None:
        """Both halves matter: lean toward the context for a misread photo, but never
        flatten a folder that legitimately holds several subjects."""
        prompt = _build_set_naming_prompt(DOCS, "Degats-eaux", None)
        self.assertIn("MISREAD photo", prompt)
        self.assertIn("Do not force one subject onto the folder", prompt)

    def test_review_is_asked_for_mixed_signals_and_not_for_mere_doubt(self) -> None:
        """Review has ONE trigger: a reading that mixes a setting excluding the file
        with a sign of the set's own subject, and nothing settling which is real.
        Measured against the live API: a generic "ask when uncertain" wording never
        fired once in nine cases, while naming this precise combination does."""
        prompt = _build_set_naming_prompt(DOCS, "Degats-eaux", None)
        self.assertIn("MIXES both", prompt)
        self.assertIn("set review to true", prompt)
        # …and everywhere else it must decide by itself rather than escalate.
        self.assertIn("JUDGE FREELY", prompt)

    def test_the_user_context_is_included_when_present(self) -> None:
        prompt = _build_set_naming_prompt(DOCS, "Degats-eaux", "Simon, plombier de metier")
        self.assertIn("Simon, plombier de metier", prompt)

    def test_no_chain_configured_renames_nothing(self) -> None:
        result = name_set(DOCS, source_folder="X", chain=[])
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.names, {})

    def test_an_invalid_reply_renames_nothing(self) -> None:
        from procrafiler.ai_naming import ChainEntry

        with patch("procrafiler.ai_set_naming.call_mistral_chat", return_value="not json at all"):
            result = name_set(
                DOCS, source_folder="X", chain=[ChainEntry("mistral", "m")], sleep_fn=lambda _s: None
            )
        self.assertTrue(result.used_fallback, "a garbled reply must not rename anything")


class TestSetNamingInPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        os.environ["PROCRAFILER_AI_NAMING_PRIMARY"] = "mistral:mistral-medium-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for key in ("PROCRAFILER_AI_ANALYSIS_PRIMARY", "PROCRAFILER_AI_NAMING_PRIMARY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _drop_set(self) -> None:
        folder = self.paths.inbox_dir / "Degats-eaux"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("a.txt", "b.txt"):
            (folder / name).write_text(f"content of {name}")

    ANALYSIS_REPLY = json.dumps(
        {"name": "Chat-sur-canape", "category_path": "Personal/Administrative/Insurance",
         "summary": "s", "keywords": []}
    )

    def test_the_set_verdict_overrides_the_per_file_name(self) -> None:
        self._drop_set()
        naming_reply = json.dumps(
            {"files": [
                {"index": 0, "name": "Degats-eaux_constat", "review": False, "why": "set context"},
                {"index": 1, "name": "Degats-eaux_facture", "review": False, "why": "set context"},
            ]}
        )
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self.ANALYSIS_REPLY):
            with patch("procrafiler.ai_set_naming.call_mistral_chat", return_value=naming_reply):
                process_all_inbox_files(self.paths, now_utc=self.now)

        filed = sorted(p.name for p in self.paths.library_root.rglob("*") if p.is_file())
        self.assertEqual(len(filed), 2)
        # The per-file analysis wanted "Chat-sur-canape" for both; the set pass won.
        for name in filed:
            self.assertIn("Degats-eaux", name)
            self.assertNotIn("Chat-sur-canape", name)

    def test_a_review_verdict_parks_the_file_in_the_decisions_queue(self) -> None:
        self._drop_set()
        naming_reply = json.dumps(
            {"files": [
                {"index": 0, "name": "Whatever", "review": True, "why": "cannot settle"},
                {"index": 1, "name": "Degats-eaux_facture", "review": False, "why": "clear"},
            ]}
        )
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self.ANALYSIS_REPLY):
            with patch("procrafiler.ai_set_naming.call_mistral_chat", return_value=naming_reply):
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["pending_decisions"], 1)
        parked = [p for p in (self.paths.library_root / "Manual_Review").rglob("*") if p.is_file()]
        self.assertEqual(len(parked), 1)

    def test_loose_root_files_are_not_put_through_the_set_pass(self) -> None:
        """A file alone at the Inbox root is not a set — no context to weigh."""
        (self.paths.inbox_dir / "alone.txt").write_text("body")
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self.ANALYSIS_REPLY):
            with patch("procrafiler.ai_set_naming.call_mistral_chat") as naming_call:
                process_all_inbox_files(self.paths, now_utc=self.now)
        naming_call.assert_not_called()

    def test_without_a_naming_chain_nothing_changes(self) -> None:
        os.environ.pop("PROCRAFILER_AI_NAMING_PRIMARY", None)
        self._drop_set()
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self.ANALYSIS_REPLY):
            with patch("procrafiler.ai_set_naming.call_mistral_chat") as naming_call:
                process_all_inbox_files(self.paths, now_utc=self.now)
        naming_call.assert_not_called()
        filed = [p.name for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertTrue(all("Chat-sur-canape" in n for n in filed), filed)


if __name__ == "__main__":
    unittest.main()
