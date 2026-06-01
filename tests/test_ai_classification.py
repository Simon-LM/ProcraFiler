from __future__ import annotations

import unittest
from unittest.mock import patch

from procrafiler.ai_classification import ClassificationResult, classify_content
from procrafiler.ai_naming import ChainEntry, ProviderCallError

BASES = ["Personnel/Documents", "Professionnel/Documents", "Administratif", "Banque"]


def _classify(content: str, **kw):  # noqa: ANN003, ANN201
    return classify_content(content, base_categories=BASES, existing_paths=[], **kw)


class TestAiClassification(unittest.TestCase):
    def test_no_chain_returns_fallback(self) -> None:
        result = _classify("Relevé de compte", chain=[])
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.path)
        self.assertEqual(result.reason, "chain_not_configured")

    def test_path_returned(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value='{"path": "Banque"}'):
            result = _classify("Relevé BNP avril 2026", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.path, "Banque")
        self.assertEqual(result.provider, "mistral")

    def test_subfolder_path_returned_verbatim(self) -> None:
        # classify_content does NOT validate the path (taxonomy does); it returns
        # whatever the model proposed, including a subfolder path.
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value='{"path":"Administratif/Impots"}'):
            result = _classify("avis d'imposition", chain=chain, retries=0)
        self.assertEqual(result.path, "Administratif/Impots")

    def test_explicit_null_path_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value='{"path": null}'):
            result = _classify("ambiguous", chain=chain, retries=0)
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.path)

    def test_failover_to_second_provider(self) -> None:
        chain = [
            ChainEntry(provider="mistral", model="mistral-small-latest"),
            ChainEntry(provider="ollama", model="mistral"),
        ]
        with patch("procrafiler.ai_classification.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            with patch("procrafiler.ai_classification.call_ollama_chat", return_value='{"path":"Administratif"}'):
                result = _classify("avis d'imposition", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.path, "Administratif")

    def test_retries_then_fallback(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        sleeps: list[int] = []
        with patch("procrafiler.ai_classification.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            result = _classify("x", chain=chain, retries=2, sleep_fn=lambda s: sleeps.append(s))
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")
        self.assertEqual(sleeps, [1, 2])

    def test_json_with_noise_is_parsed(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        noisy = 'Analyse:\n{"path": "Banque"}\nVoilà.'
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value=noisy):
            result = _classify("contenu", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.path, "Banque")

    def test_returns_classification_result_type(self) -> None:
        self.assertIsInstance(_classify("x", chain=[]), ClassificationResult)

    def test_confident_path_carries_alternatives(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        raw = '{"path": "Banque", "alternatives": ["Administratif", "Banque/Releves"]}'
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value=raw):
            result = _classify("Relevé BNP", chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.path, "Banque")
        self.assertEqual(result.alternatives, ["Administratif", "Banque/Releves"])

    def test_null_path_with_alternatives_is_uncertain_not_failure(self) -> None:
        # The AI declined to pick (null path) but offered options → this is a
        # decision-with-options, not an error: no fallback retry, options kept.
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        raw = '{"path": null, "alternatives": ["Banque", "Administratif/Impots"]}'
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value=raw) as call:
            result = _classify("ambiguous", chain=chain, retries=2)
        self.assertIsNone(result.path)
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.reason, "uncertain_with_options")
        self.assertEqual(result.alternatives, ["Banque", "Administratif/Impots"])
        self.assertEqual(call.call_count, 1)  # no wasted retries on a deliberate decline

    def test_null_path_no_alternatives_retries_then_fallback(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        sleeps: list[int] = []
        raw = '{"path": null, "alternatives": []}'
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value=raw):
            result = _classify("x", chain=chain, retries=1, sleep_fn=lambda s: sleeps.append(s))
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")
        self.assertEqual(result.alternatives, [])
        self.assertEqual(sleeps, [1])  # retried once before giving up


if __name__ == "__main__":
    unittest.main()
