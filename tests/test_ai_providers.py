from __future__ import annotations

import os
import unittest

from procrafiler.ai_naming import _ai_throttle, parse_provider_chain, task_chain_from_env


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


class TestAiThrottle(unittest.TestCase):
    """Configurable pause between real provider calls (GPU-friendly for Ollama)."""

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_THROTTLE", None)

    def _slept(self, value: str | None) -> list[float]:
        if value is None:
            os.environ.pop("PROCRAFILER_AI_THROTTLE", None)
        else:
            os.environ["PROCRAFILER_AI_THROTTLE"] = value
        calls: list[float] = []
        _ai_throttle(sleep_fn=calls.append)
        return calls

    def test_unset_does_not_sleep(self) -> None:
        self.assertEqual(self._slept(None), [])

    def test_zero_does_not_sleep(self) -> None:
        self.assertEqual(self._slept("0"), [])

    def test_invalid_does_not_sleep(self) -> None:
        self.assertEqual(self._slept("abc"), [])

    def test_positive_value_sleeps_that_long(self) -> None:
        self.assertEqual(self._slept("1.5"), [1.5])


if __name__ == "__main__":
    unittest.main()
