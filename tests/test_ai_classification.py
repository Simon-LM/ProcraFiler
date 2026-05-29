from __future__ import annotations

import unittest
from unittest.mock import patch

from procrafiler.ai_classification import ClassificationResult, classify_content
from procrafiler.ai_naming import ChainEntry, ProviderCallError

ALLOWED = ["Personnel/Documents", "Professionnel/Documents", "Administratif", "Banque"]


class TestAiClassification(unittest.TestCase):
    def test_no_chain_returns_fallback(self) -> None:
        result = classify_content("Relevé de compte", allowed_categories=ALLOWED, chain=[])
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.category)
        self.assertEqual(result.reason, "chain_not_configured")

    def test_valid_category_returned(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch(
            "procrafiler.ai_classification.call_mistral_chat",
            return_value='{"category": "Banque"}',
        ):
            result = classify_content("Relevé de compte BNP avril 2026", allowed_categories=ALLOWED, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.category, "Banque")
        self.assertEqual(result.provider, "mistral")

    def test_category_outside_allowed_list_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch(
            "procrafiler.ai_classification.call_mistral_chat",
            return_value='{"category": "Cryptomonnaie"}',
        ):
            result = classify_content("contenu", allowed_categories=ALLOWED, chain=chain, retries=0)
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.category)

    def test_explicit_null_category_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        with patch(
            "procrafiler.ai_classification.call_mistral_chat",
            return_value='{"category": null}',
        ):
            result = classify_content("ambiguous", allowed_categories=ALLOWED, chain=chain, retries=0)
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.category)

    def test_failover_to_second_provider(self) -> None:
        chain = [
            ChainEntry(provider="mistral", model="mistral-small-latest"),
            ChainEntry(provider="ollama", model="mistral"),
        ]
        with patch("procrafiler.ai_classification.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            with patch("procrafiler.ai_classification.call_ollama_chat", return_value='{"category":"Administratif"}'):
                result = classify_content("avis d'imposition", allowed_categories=ALLOWED, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.category, "Administratif")

    def test_retries_then_fallback(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        sleeps: list[int] = []
        with patch("procrafiler.ai_classification.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            result = classify_content(
                "x", allowed_categories=ALLOWED, chain=chain, retries=2, sleep_fn=lambda s: sleeps.append(s)
            )
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")
        self.assertEqual(sleeps, [1, 2])

    def test_json_with_noise_is_parsed(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-latest")]
        noisy = 'Analyse:\n{"category": "Banque"}\nVoilà.'
        with patch("procrafiler.ai_classification.call_mistral_chat", return_value=noisy):
            result = classify_content("contenu", allowed_categories=ALLOWED, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.category, "Banque")

    def test_returns_classification_result_type(self) -> None:
        result = classify_content("x", allowed_categories=ALLOWED, chain=[])
        self.assertIsInstance(result, ClassificationResult)


if __name__ == "__main__":
    unittest.main()
