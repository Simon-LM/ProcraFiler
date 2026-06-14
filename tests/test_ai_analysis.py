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
        self.assertFalse(result.series)  # absent in JSON → defaults False

    def test_series_flag_parsed_when_true(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=_full(series=True)):
            result = _analyze("Relevé BNP", chain=chain, retries=0)
        self.assertTrue(result.series)

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

    def test_prompt_name_uses_consistent_per_kind_templates(self) -> None:
        # Naming consistency: a generalist skeleton + few-shot examples so two
        # documents of the same kind get the same structure; no date in the name
        # (the timestamp prefix carries it), no redundant words.
        prompt = _build_analysis_prompt("contenu", BASES, [])
        self.assertIn("CONSISTENTLY", prompt)
        # CV pattern: underscore after CV, family NAME in UPPERCASE.
        self.assertIn("CV_<NOM>-<Prenom>", prompt)
        self.assertIn("CV_LOUVEL-Simon", prompt)
        self.assertIn("Facture_<issuer>", prompt)
        self.assertIn("Releve_<bank>", prompt)
        # Meter readings get one canonical structure so two readings of the same
        # resource get the SAME name (run 6: Releve_Compteur-eau vs Releve_eau_Compteur).
        self.assertIn("Releve_<resource>", prompt)
        self.assertIn("Releve_eau", prompt)
        self.assertIn("do NOT put a DATE in the name", prompt)

    def test_prompt_has_separator_grammar(self) -> None:
        # One generalist grammar: underscore separates semantic COMPONENTS,
        # hyphens join words WITHIN a component (run 4 leaked mixed separators).
        prompt = _build_analysis_prompt("contenu", BASES, [])
        self.assertIn("underscore separates", prompt)
        self.assertIn("hyphens join the words WITHIN", prompt)
        self.assertIn("Releve_BNP-Paribas", prompt)

    def test_prompt_series_proposes_entity_folder_without_year(self) -> None:
        # A series sets "series": true and proposes only the ENTITY folder
        # (issuer/organism, or the kind when there is no issuer) WITHOUT a year —
        # the system appends the dated year subfolder itself (run 7: the model
        # dropped/guessed the year; the code now owns it). Different entities are
        # different series → different folders.
        prompt = _build_analysis_prompt("contenu", BASES, [])
        self.assertIn('"series": true', prompt)
        self.assertIn("the system appends the dated year subfolder itself", prompt)
        self.assertIn("Utilities/EDF", prompt)
        self.assertNotIn("Utilities/EDF/2026", prompt)  # no year written by the AI
        self.assertIn("DIFFERENT entities are DIFFERENT series", prompt)

    def test_prompt_carries_filename_and_folder_as_hints(self) -> None:
        prompt = _build_analysis_prompt("x", BASES, [], original_filename="CV_Simon-LOUVEL.odt", source_folder="Dégats_eaux")
        self.assertIn("CV_Simon-LOUVEL.odt", prompt)
        self.assertIn("Dégats_eaux", prompt)
        self.assertIn("NOT ground truth", prompt)  # framed as a hint, not authority

    def test_prompt_has_no_hints_block_when_none_given(self) -> None:
        self.assertNotIn("Hints", _build_analysis_prompt("x", BASES, []))

    def test_prompt_includes_user_context_when_given(self) -> None:
        prompt = _build_analysis_prompt("x", BASES, [], user_context="La musique est un loisir")
        self.assertIn("La musique est un loisir", prompt)
        self.assertIn("About the user", prompt)
        # The Personal/Work decision is anchored to what the context DECLARES
        # (generalist: the context is the reference), and resolved by the user's
        # RELATIONSHIP — a declared hobby stays Personal even when pro-grade,
        # only the stated job leans Work (run 5 over-corrected hobby gear → Work).
        self.assertIn("DECLARED here", prompt)
        self.assertIn("RELATIONSHIP", prompt)
        self.assertIn("stated HOBBIES", prompt)
        self.assertIn("leans Work", prompt)

    def test_prompt_has_no_user_context_block_by_default(self) -> None:
        self.assertNotIn("About the user", _build_analysis_prompt("x", BASES, []))

    def test_prompt_has_series_folder_rule(self) -> None:
        # RECURRING-kind documents must get a series subfolder even when alone,
        # and reuse that folder when it already exists in the tree.
        prompt = _build_analysis_prompt("contenu", BASES, [])
        lowered = prompt.lower()
        self.assertIn("recurring", lowered)
        self.assertIn("series", lowered)

    def test_noisy_json_is_parsed(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        noisy = "Voici:\n" + _full(category_path="Personal/Administrative/Banking") + "\nVoilà."
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=noisy):
            result = _analyze("contenu", chain=chain, retries=0)
        self.assertEqual(result.category_path, "Personal/Administrative/Banking")


if __name__ == "__main__":
    unittest.main()
