from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from procrafiler.ai_naming import ChainEntry, ProviderCallError
from procrafiler.ai_organize import OrganizeResult, _build_organize_prompt, organize_set

BASES = ["Personal/Administrative/Insurance", "Personal/Administrative/Housing", "Work"]

DOCS = [
    {
        "name": "Constat amiable",
        "summary": "constat de dégâts des eaux dans la cuisine",
        "document_date": "2025-08-05",
        "category_path": "Personal/Administrative/Insurance",
    },
    {
        "name": "Photo moisissure",
        "summary": "photo de moisissure sur un mur",
        "document_date": "2025-08-07",
        "category_path": "Personal/Administrative/Insurance",
    },
]

CHAIN = [ChainEntry(provider="mistral", model="mistral-medium-latest")]


def _placements_json(pairs: list[tuple[int, str]]) -> str:
    return json.dumps({"placements": [{"index": i, "path": p} for i, p in pairs]})


class TestOrganizeSet(unittest.TestCase):
    def test_no_chain_falls_back_to_per_file_category(self) -> None:
        result = organize_set(DOCS, base_categories=BASES, existing_paths=[], chain=[])
        self.assertIsInstance(result, OrganizeResult)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "chain_not_configured")
        self.assertEqual(result.placements, {0: "Personal/Administrative/Insurance", 1: "Personal/Administrative/Insurance"})

    def test_empty_set(self) -> None:
        result = organize_set([], base_categories=BASES, existing_paths=[])
        self.assertEqual(result.placements, {})
        self.assertEqual(result.reason, "empty_set")

    def test_groups_into_a_dated_affair_folder(self) -> None:
        affair = "Personal/Administrative/Insurance/Degats-eaux-2025-08"
        raw = _placements_json([(0, affair), (1, affair)])
        with patch("procrafiler.ai_organize.call_mistral_chat", return_value=raw):
            result = organize_set(DOCS, base_categories=BASES, existing_paths=[], chain=CHAIN, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.placements[0], affair)
        self.assertEqual(result.placements[1], affair)

    def test_omitted_document_keeps_its_proposed_category(self) -> None:
        # The model only places index 0; index 1 must keep its own proposed path.
        raw = _placements_json([(0, "Personal/Administrative/Insurance/Degats-eaux-2025-08")])
        with patch("procrafiler.ai_organize.call_mistral_chat", return_value=raw):
            result = organize_set(DOCS, base_categories=BASES, existing_paths=[], chain=CHAIN, retries=0)
        self.assertEqual(result.placements[1], "Personal/Administrative/Insurance")

    def test_out_of_range_index_is_ignored(self) -> None:
        raw = _placements_json([(0, "Work"), (9, "Whatever")])
        with patch("procrafiler.ai_organize.call_mistral_chat", return_value=raw):
            result = organize_set(DOCS, base_categories=BASES, existing_paths=[], chain=CHAIN, retries=0)
        self.assertEqual(result.placements[0], "Work")
        self.assertNotIn(9, result.placements)

    def test_invalid_json_retries_then_falls_back(self) -> None:
        sleeps: list[int] = []
        with patch("procrafiler.ai_organize.call_mistral_chat", return_value="not json"):
            result = organize_set(
                DOCS, base_categories=BASES, existing_paths=[], chain=CHAIN, retries=2, sleep_fn=sleeps.append
            )
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")
        self.assertEqual(sleeps, [1, 2])
        # Fallback keeps each document's per-file proposal.
        self.assertEqual(result.placements[0], "Personal/Administrative/Insurance")

    def test_failover_to_second_provider(self) -> None:
        chain = [ChainEntry(provider="mistral", model="m"), ChainEntry(provider="ollama", model="mistral")]
        raw = _placements_json([(0, "Work"), (1, "Work")])
        with patch("procrafiler.ai_organize.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            with patch("procrafiler.ai_organize.call_ollama_chat", return_value=raw):
                result = organize_set(DOCS, base_categories=BASES, existing_paths=[], chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.provider, "ollama")

    def test_prompt_embeds_generalist_grouping_principles(self) -> None:
        prompt = _build_organize_prompt(DOCS, BASES, [], "Degats_eaux_cuisine").lower()
        self.assertIn("same affair", prompt)
        self.assertIn("recurring", prompt)
        self.assertIn("degats_eaux_cuisine", prompt)  # the drop-folder name is a signal

    def test_prompt_enforces_keep_the_set_together(self) -> None:
        # Plan B spirit (R1–R5): a dropped folder stays together by default; only a
        # FLAGRANT misfit is split out; photo descriptions may be hallucinated; the
        # date goes at the START of the folder name.
        prompt = _build_organize_prompt(DOCS, BASES, [], "Degats_eaux_cuisine").lower()
        self.assertIn("strong hypothesis", prompt)
        self.assertIn("keep the set together", prompt)       # R1
        self.assertIn("flagrantly", prompt)                  # R2 — high bar to split
        self.assertIn("two base categories", prompt)         # R3 — no cross-base split
        self.assertIn("start of the folder name", prompt)    # R4 — date first
        self.assertIn("hallucinate", prompt)                 # R5 — photos unreliable
        # Only one-off AFFAIRS are dated; a SERIES folder stays undated, a
        # period inside it is a bare-year subfolder.
        self.assertIn("series folder (recurring kind) is never dated", prompt)
        self.assertIn("releves-eau/2026", prompt)

    def test_prompt_has_no_hypothesis_block_without_a_drop_folder(self) -> None:
        prompt = _build_organize_prompt(DOCS, BASES, [], None)
        self.assertNotIn("STRONG HYPOTHESIS", prompt)

    def test_prompt_includes_user_context(self) -> None:
        prompt = _build_organize_prompt(DOCS, BASES, [], "Degats_eaux", user_context="Musique = loisir")
        self.assertIn("Musique = loisir", prompt)
        self.assertIn("Context about the user", prompt)

    def test_prompt_has_no_user_context_block_by_default(self) -> None:
        self.assertNotIn("Context about the user", _build_organize_prompt(DOCS, BASES, [], "Degats_eaux"))


if __name__ == "__main__":
    unittest.main()
