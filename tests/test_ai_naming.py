from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from procrafiler.ai_naming import (
    ChainEntry,
    ProviderCallError,
    default_chain_from_env,
    parse_provider_chain,
    suggest_stem_with_ai,
)


class TestAiNaming(unittest.TestCase):
    def test_parse_provider_chain_splits_only_first_colon(self) -> None:
        chain = parse_provider_chain("mistral:mistral-small-2506,huggingface:openai/gpt-oss-120b:ovhcloud")
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].provider, "mistral")
        self.assertEqual(chain[0].model, "mistral-small-2506")
        self.assertEqual(chain[1].provider, "huggingface")
        self.assertEqual(chain[1].model, "openai/gpt-oss-120b:ovhcloud")

    def test_default_chain_from_task_primary_and_fallback(self) -> None:
        os.environ["PROCRAFILER_AI_NAMING_PRIMARY"] = "mistral:mistral-small-2506"
        os.environ["PROCRAFILER_AI_NAMING_FALLBACK"] = "ollama:mistral"

        chain = default_chain_from_env()

        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].provider, "mistral")
        self.assertEqual(chain[1].provider, "ollama")

        os.environ.pop("PROCRAFILER_AI_NAMING_PRIMARY", None)
        os.environ.pop("PROCRAFILER_AI_NAMING_FALLBACK", None)

    def test_no_chain_returns_safe_fallback(self) -> None:
        suggestion = suggest_stem_with_ai("Facture Avril 2026.pdf", chain=[])
        self.assertTrue(suggestion.used_fallback)
        self.assertEqual(suggestion.reason, "chain_not_configured")
        self.assertEqual(suggestion.stem, "Facture-Avril-2026")

    def test_failover_to_second_provider(self) -> None:
        chain = [
            ChainEntry(provider="mistral", model="mistral-small-2506"),
            ChainEntry(provider="ollama", model="mistral"),
        ]

        with patch("procrafiler.ai_naming.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            with patch(
                "procrafiler.ai_naming.call_ollama_chat",
                return_value='{"stem":"Compte rendu reunion"}',
            ):
                suggestion = suggest_stem_with_ai("meeting-notes.txt", chain=chain, retries=0)

        self.assertFalse(suggestion.used_fallback)
        self.assertEqual(suggestion.provider, "ollama")
        self.assertEqual(suggestion.stem, "Compte-rendu-reunion")

    def test_retries_before_fallback(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-2506")]
        sleep_calls: list[int] = []

        def fake_sleep(seconds: int) -> None:
            sleep_calls.append(seconds)

        with patch("procrafiler.ai_naming.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")):
            suggestion = suggest_stem_with_ai("scan.png", chain=chain, retries=2, sleep_fn=fake_sleep)

        self.assertTrue(suggestion.used_fallback)
        self.assertEqual(suggestion.provider, "fallback")
        self.assertEqual(sleep_calls, [1, 2])

    def test_json_with_prefix_suffix_is_parsed(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-2506")]
        noisy = 'Analyse:\n{"stem":"Facture EDF Avril"}\nFin.'

        with patch("procrafiler.ai_naming.call_mistral_chat", return_value=noisy):
            suggestion = suggest_stem_with_ai("facture-edf-2026.pdf", chain=chain, retries=0)

        self.assertFalse(suggestion.used_fallback)
        self.assertEqual(suggestion.stem, "Facture-EDF-Avril")

    def test_json_fenced_block_is_parsed(self) -> None:
        chain = [ChainEntry(provider="ollama", model="mistral")]
        fenced = '```json\n{"stem":"Releve bancaire mars"}\n```'

        with patch("procrafiler.ai_naming.call_ollama_chat", return_value=fenced):
            suggestion = suggest_stem_with_ai("releve.pdf", chain=chain, retries=0)

        self.assertFalse(suggestion.used_fallback)
        self.assertEqual(suggestion.stem, "Releve-bancaire-mars")

    def test_invalid_json_response_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-small-2506")]

        with patch("procrafiler.ai_naming.call_mistral_chat", return_value="nom libre sans json"):
            suggestion = suggest_stem_with_ai("scan-contrat.png", chain=chain, retries=0)

        self.assertTrue(suggestion.used_fallback)
        self.assertEqual(suggestion.stem, "scan-contrat")

    def test_task_level_timeout_and_retry_override(self) -> None:
        os.environ["PROCRAFILER_AI_NAMING_TIMEOUT"] = "42"
        os.environ["PROCRAFILER_AI_NAMING_RETRIES"] = "1"
        chain = [ChainEntry(provider="mistral", model="mistral-small-2506")]

        with patch("procrafiler.ai_naming.call_mistral_chat", side_effect=ProviderCallError("API_ERROR_500")) as mocked:
            suggestion = suggest_stem_with_ai("scan-contrat.png", chain=chain)

        self.assertTrue(suggestion.used_fallback)
        self.assertEqual(mocked.call_count, 2)
        timeout_values = [call.kwargs.get("timeout") for call in mocked.call_args_list]
        self.assertTrue(all(value == 42 for value in timeout_values))

        os.environ.pop("PROCRAFILER_AI_NAMING_TIMEOUT", None)
        os.environ.pop("PROCRAFILER_AI_NAMING_RETRIES", None)


if __name__ == "__main__":
    unittest.main()