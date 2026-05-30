from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from procrafiler.ai_naming import ChainEntry, ProviderCallError, RateLimitedError
from procrafiler.ai_reader import (
    AIReadResult,
    _extract_ocr_text,
    call_mistral_ocr,
    read_with_ocr,
)


class TestExtractOcrText(unittest.TestCase):
    def test_concatenates_page_markdown(self) -> None:
        body = {"pages": [{"markdown": "Page one"}, {"markdown": "Page two"}, {"markdown": ""}]}
        self.assertEqual(_extract_ocr_text(body), "Page one\n\nPage two")

    def test_bad_shape_raises(self) -> None:
        with self.assertRaises(ProviderCallError):
            _extract_ocr_text({"no_pages": True})


class TestCallMistralOcr(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "scan.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake bytes")
        os.environ["MISTRAL_API_KEY"] = "test-key"

    def tearDown(self) -> None:
        os.environ.pop("MISTRAL_API_KEY", None)
        self.tmp.cleanup()

    def test_builds_data_uri_payload_and_parses(self) -> None:
        captured: dict[str, object] = {}

        def fake_post(url, payload, headers, timeout):  # noqa: ANN001, ANN202
            captured["url"] = url
            captured["payload"] = payload
            return 200, b'{"pages": [{"markdown": "Releve BNP"}]}'

        with patch("procrafiler.ai_reader._post_json", side_effect=fake_post):
            text = call_mistral_ocr(self.pdf, "mistral-ocr-latest")

        self.assertEqual(text, "Releve BNP")
        self.assertEqual(captured["url"], "https://api.mistral.ai/v1/ocr")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["model"], "mistral-ocr-latest")
        self.assertEqual(payload["document"]["type"], "document_url")
        self.assertTrue(payload["document"]["document_url"].startswith("data:application/pdf;base64,"))

    def test_missing_api_key_raises(self) -> None:
        os.environ.pop("MISTRAL_API_KEY", None)
        with self.assertRaises(ProviderCallError):
            call_mistral_ocr(self.pdf, "mistral-ocr-latest")

    def test_http_error_raises(self) -> None:
        with patch("procrafiler.ai_reader._post_json", return_value=(500, b'{"error":"boom"}')):
            with self.assertRaises(ProviderCallError):
                call_mistral_ocr(self.pdf, "mistral-ocr-latest")

    def test_rate_limit_raises(self) -> None:
        with patch("procrafiler.ai_reader._post_json", return_value=(429, b"{}")):
            with self.assertRaises(RateLimitedError):
                call_mistral_ocr(self.pdf, "mistral-ocr-latest")


class TestReadWithOcr(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "scan.pdf"
        self.pdf.write_bytes(b"%PDF fake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_chain_returns_fallback(self) -> None:
        result = read_with_ocr(self.pdf, chain=[])
        self.assertIsInstance(result, AIReadResult)
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.text)
        self.assertEqual(result.reason, "chain_not_configured")

    def test_success(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-ocr-latest")]
        with patch("procrafiler.ai_reader.call_mistral_ocr", return_value="Releve de compte BNP"):
            result = read_with_ocr(self.pdf, chain=chain, retries=0)
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.text, "Releve de compte BNP")
        self.assertEqual(result.provider, "mistral")

    def test_empty_result_falls_back(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-ocr-latest")]
        with patch("procrafiler.ai_reader.call_mistral_ocr", return_value="   "):
            result = read_with_ocr(self.pdf, chain=chain, retries=0)
        self.assertTrue(result.used_fallback)
        self.assertIsNone(result.text)

    def test_retries_then_fallback(self) -> None:
        chain = [ChainEntry(provider="mistral", model="mistral-ocr-latest")]
        sleeps: list[int] = []
        with patch("procrafiler.ai_reader.call_mistral_ocr", side_effect=ProviderCallError("OCR_API_ERROR_500")):
            result = read_with_ocr(self.pdf, chain=chain, retries=2, sleep_fn=lambda s: sleeps.append(s))
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.provider, "fallback")
        self.assertEqual(sleeps, [1, 2])


if __name__ == "__main__":
    unittest.main()
