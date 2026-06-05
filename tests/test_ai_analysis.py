from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from procrafiler.ai_analysis import AnalysisResult, _build_analysis_prompt, analyze_content
from procrafiler.ai_naming import ChainEntry, ProviderCallError

BASES = ["Personal", "Work", "Personal/Administrative", "Personal/Administrative/Banking"]


def _analyze(content: str, **kw):  # noqa: ANN003, ANN201
    return analyze_content(content, base_categories=BASES, existing_paths=[], **kw)


def _full(**overrides: object) -> str:
    payload = {
        "name": "Releve BNP avril 2026",
        "date": "2026-04-30",
        "category_path": "Personal/Administrative/Banking",
        "alternatives": ["Personal/Administrative"],
        "summary": "Relevé de compte BNP pour avril 2026.",
        "keywords": ["banque", "bnp", "relevé"],
        "entities": {"issuer": "BNP Paribas", "doc_type": "relevé"},
        "language": "fr",
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestAnalyzeContent(unittest.TestCase):
    def test_no_chain_returns_fallback(self) -> None:
        result = _analyze("Relevé de compte", chain=[])
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "chain_not_configured")
        self.assertIsNone(result.name)
        self.assertIsNone(result.category_path)
        self.assertEqual(result.keywords, [])

    def test_no_content_does_not_call_ai(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_analysis.call_mistral_chat") as mocked:
            result = _analyze("   ", chain=chain)
        mocked.assert_not_called()
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "no_content")

    def test_full_fiche_parsed(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=_full()):
            result = _analyze("Relevé BNP avril 2026", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.name, "Releve-BNP-avril-2026")  # sanitized stem
        self.assertEqual(result.document_date, "2026-04-30")
        self.assertEqual(result.category_path, "Personal/Administrative/Banking")
        self.assertEqual(result.alternatives, ["Personal/Administrative"])
        self.assertEqual(result.summary, "Relevé de compte BNP pour avril 2026.")
        self.assertEqual(result.keywords, ["banque", "bnp", "relevé"])
        self.assertEqual(result.entities["issuer"], "BNP Paribas")
        self.assertEqual(result.language, "fr")

    def test_name_from_content_not_filename(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        captured: dict[str, str] = {}

        def fake_call(prompt: str, model: str, **kwargs: object) -> str:
            captured["prompt"] = prompt
            return _full(name="Compte rendu reunion")

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=fake_call):
            result = _analyze("Notes BNP Paribas avril", chain=chain, retries=0)
        self.assertEqual(result.name, "Compte-rendu-reunion")
        self.assertIn("BNP Paribas", captured["prompt"])  # prompt carried the content

    def test_null_category_with_alternatives_is_still_a_success(self) -> None:
        # The AI declined to pick a category but produced a fiche + options. That
        # is a valid result (used_fallback False); routing is the caller's call.
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        raw = _full(category_path=None, alternatives=["Personal/Administrative/Banking", "Personal/Administrative"])
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=raw):
            result = _analyze("ambiguous", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertIsNone(result.category_path)
        self.assertEqual(result.alternatives, ["Personal/Administrative/Banking", "Personal/Administrative"])
        self.assertIsNotNone(result.summary)  # metadata still captured

    def test_bad_date_and_missing_fields_degrade_gracefully(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        raw = json.dumps({"name": "X", "date": "30/04/2026"})  # bad date, no other fields
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=raw):
            result = _analyze("x", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.name, "X")
        self.assertIsNone(result.document_date)  # invalid format dropped
        self.assertEqual(result.keywords, [])
        self.assertEqual(result.entities, {})

    def test_invalid_json_retries_then_fails_over(self) -> None:
        chain = [
            ChainEntry(provider="mistral", model="mistral-small-latest"),
            ChainEntry(provider="ollama", model="mistral"),
        ]
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value="no json here"):
            with patch("procrafiler.ai_analysis.call_ollama_chat", return_value=_full(category_path="Personal/Administrative")):
                result = _analyze("x", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.category_path, "Personal/Administrative")

    def test_retries_then_fallback(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        sleeps: list[int] = []
        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            result = _analyze("x", chain=chain, retries=2, sleep_fn=lambda s: sleeps.append(s))
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")
        self.assertEqual(sleeps, [1, 2])

    def test_prompt_names_by_salient_entity_not_type(self) -> None:
        # Generalist naming rule (no per-type hardcoding): name by the
        # distinctive entity (who/what), never by file type/format.
        prompt = _build_analysis_prompt("contenu", BASES, [])
        lowered = prompt.lower()
        self.assertIn("most distinctive entity", lowered)
        self.assertIn("do not name it by its file type or format", lowered)

    def test_prompt_carries_filename_and_folder_as_hints(self) -> None:
        prompt = _build_analysis_prompt("x", BASES, [], original_filename="CV_Simon-LOUVEL.odt", source_folder="Dégats_eaux")
        self.assertIn("CV_Simon-LOUVEL.odt", prompt)
        self.assertIn("Dégats_eaux", prompt)
        self.assertIn("NOT ground truth", prompt)  # framed as a hint, not authority

    def test_prompt_has_no_hints_block_when_none_given(self) -> None:
        self.assertNotIn("Hints", _build_analysis_prompt("x", BASES, []))

    def test_noisy_json_is_parsed(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        noisy = "Voici:\n" + _full(category_path="Personal/Administrative/Banking") + "\nVoilà."
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=noisy):
            result = _analyze("contenu", chain=chain, retries=0)
        self.assertEqual(result.category_path, "Personal/Administrative/Banking")


if __name__ == "__main__":
    unittest.main()
