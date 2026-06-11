from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from procrafiler.ai_grouping import GroupingResult, _build_grouping_prompt, propose_grouping
from procrafiler.ai_naming import ChainEntry, ProviderCallError

BRANCHES_ONE = {"Personal/Administrative/Housing": ["2026-01-15__Releve-eau-jan.pdf"]}
BRANCHES_TWO = {
    "Personal/Administrative/Housing": ["2026-01-15__Releve-eau-jan.pdf", "2026-02-10__Releve-eau-feb.pdf"],
    "Personal/Administrative/Banking": ["2026-03-01__Releve-BNP-mars.pdf"],
}
DOC = {"name": "Releve-eau-mars", "summary": "relevé compteur eau mars 2026", "original_filename": "releve_mars.pdf"}


class TestProposeGrouping(unittest.TestCase):
    def test_empty_dict_returns_fallback(self) -> None:
        result = propose_grouping(DOC, {})
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "no_candidate_files")
        self.assertIsNone(result.path)
        self.assertEqual(result.group_with, [])

    def test_all_empty_branches_returns_fallback(self) -> None:
        result = propose_grouping(DOC, {"Personal/Administrative/Housing": []})
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "no_candidate_files")

    def test_no_chain_returns_fallback(self) -> None:
        result = propose_grouping(DOC, BRANCHES_ONE, chain=[])
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "chain_not_configured")

    def test_full_result_with_path_and_group_with(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        raw = json.dumps(
            {"path": "Personal/Administrative/Housing/Releves-eau", "group_with": ["2026-01-15__Releve-eau-jan.pdf"]}
        )
        with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=raw):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.path, "Personal/Administrative/Housing/Releves-eau")
        self.assertEqual(result.group_with, ["2026-01-15__Releve-eau-jan.pdf"])
        self.assertEqual(result.provider, "mistral")

    def test_null_path_with_empty_group_with(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        raw = json.dumps({"path": None, "group_with": []})
        with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=raw):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertIsNone(result.path)
        self.assertEqual(result.group_with, [])

    def test_invalid_json_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_grouping.call_mistral_chat", return_value="not json at all"):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertTrue(result.used_fallback)

    def test_api_error_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_grouping.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")


class TestBuildGroupingPrompt(unittest.TestCase):
    def test_prompt_contains_existing_filenames(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_TWO)
        self.assertIn("2026-01-15__Releve-eau-jan.pdf", prompt)
        self.assertIn("2026-02-10__Releve-eau-feb.pdf", prompt)
        self.assertIn("2026-03-01__Releve-BNP-mars.pdf", prompt)

    def test_prompt_contains_branch_paths(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("Personal/Administrative/Housing", prompt)

    def test_prompt_enforces_date_at_start_rule(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        lowered = prompt.lower()
        # The prompt must say the date goes at the START (never at the end).
        self.assertIn("start", lowered)
        self.assertIn("never at the end", lowered)

    def test_prompt_has_high_bar_rule(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("HIGH BAR", prompt)

    def test_prompt_includes_document_name_and_summary(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("Releve-eau-mars", prompt)
        self.assertIn("compteur eau", prompt)


if __name__ == "__main__":
    unittest.main()
