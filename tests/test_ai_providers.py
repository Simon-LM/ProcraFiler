from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from procrafiler.ai_naming import (
    _ai_sampling_params,
    _ai_throttle,
    _task_timeout_from_env,
    call_mistral_chat,
    parse_provider_chain,
    task_chain_from_env,
)


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


class TestProviderAwareTimeout(unittest.TestCase):
    _VARS = ("PROCRAFILER_AI_TIMEOUT", "PROCRAFILER_AI_LOCAL_TIMEOUT", "PROCRAFILER_AI_ANALYSIS_TIMEOUT")

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        for k in self._VARS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _t(self, provider: str | None) -> int:
        return _task_timeout_from_env("ANALYSIS", default_value=60, provider=provider)

    def test_defaults_moderate_for_api_generous_for_local(self) -> None:
        self.assertEqual(self._t("mistral"), 60)
        self.assertEqual(self._t(None), 60)
        self.assertGreaterEqual(self._t("ollama"), 900)

    def test_api_knob_only_affects_the_api(self) -> None:
        os.environ["PROCRAFILER_AI_TIMEOUT"] = "30"
        self.assertEqual(self._t("mistral"), 30)
        self.assertGreaterEqual(self._t("ollama"), 900)  # local unaffected by the API knob

    def test_local_knob_only_affects_local(self) -> None:
        os.environ["PROCRAFILER_AI_LOCAL_TIMEOUT"] = "1500"
        self.assertEqual(self._t("ollama"), 1500)
        self.assertEqual(self._t("mistral"), 60)  # API unaffected by the local knob

    def test_per_task_override_wins_for_either_provider(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_TIMEOUT"] = "1200"
        self.assertEqual(self._t("ollama"), 1200)
        self.assertEqual(self._t("mistral"), 1200)


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


class TestSamplingParams(unittest.TestCase):
    """Temperature/top_p are configurable via the env; NEUTRAL (nothing sent) by
    default so the model uses its own default — the reference baseline."""

    def tearDown(self) -> None:
        for key in ("PROCRAFILER_AI_TEMPERATURE", "PROCRAFILER_AI_TOP_P", "MISTRAL_API_KEY"):
            os.environ.pop(key, None)

    def test_unset_sends_nothing(self) -> None:
        self.assertEqual(_ai_sampling_params(), {})

    def test_temperature_and_top_p_parsed(self) -> None:
        os.environ["PROCRAFILER_AI_TEMPERATURE"] = "0.3"
        os.environ["PROCRAFILER_AI_TOP_P"] = "0.9"
        self.assertEqual(_ai_sampling_params(), {"temperature": 0.3, "top_p": 0.9})

    def test_invalid_value_is_ignored(self) -> None:
        os.environ["PROCRAFILER_AI_TEMPERATURE"] = "abc"
        self.assertEqual(_ai_sampling_params(), {})

    def _capture_payload(self) -> dict:
        captured: dict = {}

        def fake_post(url, payload, headers, timeout):  # noqa: ANN001, ANN202
            captured["payload"] = payload
            return 200, b'{"choices":[{"message":{"content":"ok"}}]}'

        os.environ["MISTRAL_API_KEY"] = "test-key"
        with patch("procrafiler.ai_naming._post_json", side_effect=fake_post):
            call_mistral_chat("hi", "mistral-medium-latest", timeout=5)
        return captured["payload"]

    def test_call_omits_temperature_by_default(self) -> None:
        payload = self._capture_payload()
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)

    def test_call_applies_env_temperature(self) -> None:
        os.environ["PROCRAFILER_AI_TEMPERATURE"] = "0.3"
        payload = self._capture_payload()
        self.assertEqual(payload["temperature"], 0.3)


if __name__ == "__main__":
    unittest.main()
