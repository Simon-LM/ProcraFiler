from __future__ import annotations

import os
import unittest

from procrafiler.ai_naming import parse_provider_chain, task_chain_from_env


class TestProviderPlumbing(unittest.TestCase):
    def test_parse_provider_chain_splits_only_first_colon(self) -> None:
        chain = parse_provider_chain("mistral:mistral-small-2506,huggingface:openai/gpt-oss-120b:ovhcloud")
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].provider, "mistral")
        self.assertEqual(chain[0].model, "mistral-small-2506")
        self.assertEqual(chain[1].provider, "huggingface")
        self.assertEqual(chain[1].model, "openai/gpt-oss-120b:ovhcloud")

    def test_parse_provider_chain_skips_malformed_tokens(self) -> None:
        chain = parse_provider_chain("  , no-colon , mistral:m ")
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].provider, "mistral")
        self.assertEqual(chain[0].model, "m")

    def test_task_chain_from_env_primary_then_fallback(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-2506"
        os.environ["PROCRAFILER_AI_ANALYSIS_FALLBACK"] = "ollama:mistral"
        try:
            chain = task_chain_from_env("ANALYSIS")
        finally:
            os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
            os.environ.pop("PROCRAFILER_AI_ANALYSIS_FALLBACK", None)

        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].provider, "mistral")
        self.assertEqual(chain[1].provider, "ollama")

    def test_unknown_task_has_no_chain(self) -> None:
        # NAMING / CLASSIFICATION were merged into ANALYSIS and are no longer tasks.
        self.assertEqual(task_chain_from_env("NAMING"), [])
        self.assertEqual(task_chain_from_env("CLASSIFICATION"), [])


if __name__ == "__main__":
    unittest.main()
