from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from procrafiler.ai_grouping import _build_grouping_prompt, propose_grouping
from procrafiler.ai_naming import ChainEntry, ProviderCallError

# Branch listings are branch-RELATIVE paths: the model sees where inside the
# branch each file lives (an existing series subfolder is visible as such).
BRANCHES_ONE = {"Personal/Administrative/Housing": ["2026-01-15__Releve-eau-jan.pdf"]}
BRANCHES_TWO = {
    "Personal/Administrative/Housing": [
        "Releves-eau/2026-01-15__Releve-eau-jan.pdf",
        "2026-02-10__Releve-eau-feb.pdf",
    ],
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

    def test_default_chain_is_the_organize_task(self) -> None:
        # G6: this judgment (moving already-filed documents) deserves the same
        # model as the set organizer — the chain comes from ORGANIZE, not ANALYSIS.
        with patch("procrafiler.ai_grouping.task_chain_from_env", return_value=[]) as chain_mock:
            propose_grouping(DOC, BRANCHES_ONE)
        chain_mock.assert_called_once_with("ORGANIZE")

    def test_full_result_with_path_and_group_with(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-medium-latest")]
        raw = json.dumps(
            {"path": "Personal/Administrative/Housing/Releves-eau", "group_with": ["2026-01-15__Releve-eau-jan.pdf"]}
        )
        with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=raw):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.path, "Personal/Administrative/Housing/Releves-eau")
        self.assertEqual(result.group_with, ["2026-01-15__Releve-eau-jan.pdf"])
        self.assertIsNone(result.name)  # absent in JSON → keep the analysis name
        self.assertEqual(result.provider, "mistral")

    def test_name_is_parsed_when_present(self) -> None:
        # 3a: the model returns a consistent stem for the new file joining a series.
        chain = [ChainEntry(provider="mistral", model="mistral-medium-latest")]
        raw = json.dumps(
            {"path": "Personal/Administrative/Housing/Releves-eau", "group_with": [], "name": "Releve_eau"}
        )
        with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=raw):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertEqual(result.name, "Releve_eau")

    def test_null_path_with_empty_group_with(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-medium-latest")]
        raw = json.dumps({"path": None, "group_with": []})
        with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=raw):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertIsNone(result.path)
        self.assertEqual(result.group_with, [])

    def test_invalid_json_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-medium-latest")]
        with patch("procrafiler.ai_grouping.call_mistral_chat", return_value="not json at all"):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertTrue(result.used_fallback)

    def test_api_error_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-medium-latest")]
        with patch("procrafiler.ai_grouping.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            result = propose_grouping(DOC, BRANCHES_ONE, chain=chain, retries=0)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")


class TestBuildGroupingPrompt(unittest.TestCase):
    def test_prompt_contains_relative_paths(self) -> None:
        # G1: the listing carries branch-relative PATHS, so an existing series
        # subfolder is visible (Releves-eau/…) and citations are unambiguous.
        prompt = _build_grouping_prompt(DOC, BRANCHES_TWO)
        self.assertIn("Releves-eau/2026-01-15__Releve-eau-jan.pdf", prompt)
        self.assertIn("2026-02-10__Releve-eau-feb.pdf", prompt)
        self.assertIn("2026-03-01__Releve-BNP-mars.pdf", prompt)

    def test_prompt_contains_branch_paths(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("Personal/Administrative/Housing", prompt)

    def test_prompt_states_the_deepen_only_contract(self) -> None:
        # G7: the prompt describes what the pipeline locks enforce — subfolders
        # only ever go DEEPER; never a parent, never a sibling branch.
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("DEEPER", prompt)
        self.assertIn("NEVER propose a parent folder", prompt)
        self.assertIn("never up, never sideways", prompt)

    def test_prompt_enforces_date_at_start_rule(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("DATE at the START", prompt)
        self.assertIn("NEVER at the end", prompt)

    def test_prompt_says_series_folders_are_never_dated(self) -> None:
        # Run 4 produced a wrongly dated series folder (2026_Releves-eau holding
        # 2024/2025 readings) because the example here taught dating series.
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("SERIES subfolder is named after its ENTITY", prompt)
        self.assertIn("is NEVER dated", prompt)
        # The AI proposes the entity folder WITHOUT a year; the system dates it.
        self.assertIn("the system appends the dated year subfolder itself", prompt)
        self.assertIn("Releves-eau/2024", prompt)
        self.assertNotIn("Releves-eau/2026", prompt)

    def test_prompt_forbids_grouping_across_entities(self) -> None:
        # Run 6: an Enercoop bill was pulled into Energy/EDF. Different issuers
        # are different series and must never share a folder.
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("DIFFERENT entities are DIFFERENT series", prompt)
        self.assertIn("an EDF bill and an Enercoop bill", prompt)

    def test_prompt_asks_for_consistent_name_when_joining_a_series(self) -> None:
        # 3a: when the new file joins a populated series, name it like its
        # siblings so the series stays internally consistent.
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("\"name\"", prompt)
        self.assertIn("SAME structure as those existing files", prompt)

    def test_prompt_has_high_bar_rule(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("HIGH BAR", prompt)

    def test_prompt_high_bar_requires_same_specific_subject(self) -> None:
        # Bug 3 (run 8): unrelated items under the same branch (a sound mixer vs
        # a codev workshop, both Hobbies) must NOT be nested — same SPECIFIC
        # subject only.
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("SAME SPECIFIC subject", prompt)
        self.assertIn("separate siblings", prompt)

    def test_prompt_shared_folder_named_by_common_subject(self) -> None:
        # Run 9: a shared folder is named for the COMMON kind, never one item's
        # brand (an SWR amp + a Soundcraft mixer → "Materiel-audio", not "SWR").
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("NAME A SHARED FOLDER BY WHAT ITS FILES HAVE IN COMMON", prompt)
        self.assertIn("Materiel-audio", prompt)

    def test_prompt_includes_document_name_and_summary(self) -> None:
        prompt = _build_grouping_prompt(DOC, BRANCHES_ONE)
        self.assertIn("Releve-eau-mars", prompt)
        self.assertIn("compteur eau", prompt)


if __name__ == "__main__":
    unittest.main()
