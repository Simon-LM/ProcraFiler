# pyright: reportUnknownVariableType=false
"""Knowing what a run really consumed, instead of how many calls it made.

The estimator answered "how much work is this batch", and that was quietly sold as
"how much will this cost". Two things were wrong with it. It counted a free local
Ollama call exactly like a billed Mistral one, so a fully-local install saw a
number that read like an invoice and was in fact zero. And nothing was ever
measured: every provider response carries the tokens it used, and the app parsed
that block off the wire and dropped it.

These tests pin both halves — the measurement being taken and kept, and the
forecast distinguishing who serves the call.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.ai_estimate import estimate_ai_calls, format_estimate
from procrafiler.usage_meter import (
    RunUsage,
    current_usage,
    extract_units,
    format_usage_report,
    record_response,
    usage_scope,
)

CHAIN_VARS = (
    "PROCRAFILER_AI_IMAGE_PRIMARY",
    "PROCRAFILER_AI_OCR_PRIMARY",
    "PROCRAFILER_AI_ANALYSIS_PRIMARY",
    "PROCRAFILER_AI_NAMING_PRIMARY",
    "PROCRAFILER_AI_ORGANIZE_PRIMARY",
)


class ExtractUnitsTests(unittest.TestCase):
    """Three providers, three response shapes, one extractor."""

    def test_mistral_chat_usage_block(self) -> None:
        units = extract_units({"choices": [], "usage": {"prompt_tokens": 1847, "completion_tokens": 213}})
        self.assertEqual((units.tokens_in, units.tokens_out, units.pages), (1847, 213, 0))
        self.assertTrue(units.measured)

    def test_mistral_ocr_reports_pages_not_tokens(self) -> None:
        """OCR is billed per page. Reading its count as tokens, or ignoring it,
        would leave the one exactly-knowable cost in the app unmeasured."""
        units = extract_units({"pages": [], "usage_info": {"pages_processed": 12, "doc_size_bytes": 900}})
        self.assertEqual(units.pages, 12)
        self.assertEqual((units.tokens_in, units.tokens_out), (0, 0))
        self.assertTrue(units.measured)

    def test_ollama_reports_at_top_level(self) -> None:
        units = extract_units(
            {"message": {"content": "x"}, "prompt_eval_count": 900, "eval_count": 64, "done": True}
        )
        self.assertEqual((units.tokens_in, units.tokens_out), (900, 64))
        self.assertTrue(units.measured)

    def test_unknown_shape_is_unmeasured_not_zero(self) -> None:
        """"We do not know what this cost" and "this cost nothing" must not collapse
        into the same answer — one of them silently understates a bill."""
        units = extract_units({"choices": [{"x": 1}]})
        self.assertEqual((units.tokens_in, units.tokens_out, units.pages), (0, 0, 0))
        self.assertFalse(units.measured)

    def test_non_numeric_counts_do_not_poison_totals(self) -> None:
        units = extract_units({"usage_info": {"pages_processed": "not a number"}})
        self.assertEqual(units.pages, 0)
        self.assertTrue(units.measured)  # the field was present, merely unusable

    def test_transcription_reports_seconds_of_audio(self) -> None:
        """Voxtral bills per second of audio, and its reply ALSO carries token
        counts that are not the billing basis. Both are kept; only the price table
        decides which one costs money. Reading the tokens as the bill would price
        an hour of speech at a few cents' worth of text."""
        units = extract_units(
            {"text": "hello", "usage": {"prompt_audio_seconds": 7, "prompt_tokens": 4, "completion_tokens": 62}}
        )
        self.assertEqual(units.audio_seconds, 7)
        self.assertEqual((units.tokens_in, units.tokens_out), (4, 62))
        self.assertTrue(units.measured)

    def test_garbage_body_never_raises(self) -> None:
        for body in (None, "a string", 42, [], {"usage": "not a dict"}):
            with self.subTest(body=body):
                units = extract_units(body)
                self.assertEqual(
                    (units.tokens_in, units.tokens_out, units.pages, units.audio_seconds), (0, 0, 0, 0)
                )
                self.assertFalse(units.measured)


class RecordingTests(unittest.TestCase):
    def test_aggregates_repeated_calls_of_the_same_kind(self) -> None:
        with usage_scope() as usage:
            for _ in range(3):
                record_response("mistral", "m", "ANALYSIS", {"usage": {"prompt_tokens": 100, "completion_tokens": 10}})
            entries = usage.entries()
        self.assertEqual(len(entries), 1, "one line per (provider, model, task), not per call")
        self.assertEqual(entries[0].calls, 3)
        self.assertEqual((entries[0].tokens_in, entries[0].tokens_out), (300, 30))

    def test_local_calls_are_counted_but_never_billed(self) -> None:
        with usage_scope() as usage:
            record_response("mistral", "m", "ANALYSIS", {"usage": {"prompt_tokens": 100, "completion_tokens": 0}})
            record_response("ollama", "llava", "IMAGE", {"prompt_eval_count": 5000, "eval_count": 200})
        self.assertEqual(usage.total_calls, 2)
        self.assertEqual(usage.billed_calls, 1)
        self.assertEqual(usage.local_calls, 1)
        self.assertEqual(usage.total_tokens, 100, "the 5200 local tokens are not billable volume")

    def test_unmeasured_calls_are_flagged_separately(self) -> None:
        with usage_scope() as usage:
            record_response("mistral", "m", "OCR", {"nothing": "recognisable"})
            entry = usage.entries()[0]
        self.assertEqual(entry.calls, 1)
        self.assertEqual(entry.unmeasured_calls, 1)
        self.assertIn("volume not reported", format_usage_report(usage))

    def test_recording_outside_a_scope_is_a_silent_no_op(self) -> None:
        """A unit test or a one-off script calls the providers without opening a
        meter; that must not raise, and must not leak into the next run."""
        self.assertIsNone(current_usage())
        record_response("mistral", "m", "ANALYSIS", {"usage": {"prompt_tokens": 1}})
        self.assertIsNone(current_usage())

    def test_scope_is_restored_even_when_the_run_raises(self) -> None:
        outer = RunUsage()
        with usage_scope(outer):
            try:
                with usage_scope():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            self.assertIs(current_usage(), outer)
        self.assertIsNone(current_usage())

    def test_report_marks_free_providers(self) -> None:
        with usage_scope() as usage:
            record_response("ollama", "llava", "IMAGE", {"prompt_eval_count": 10, "eval_count": 2})
        report = format_usage_report(usage)
        self.assertIn("local, free", report)
        self.assertNotIn("Billable total", report)

    def test_report_on_an_untouched_run(self) -> None:
        self.assertIn("no provider call", format_usage_report(RunUsage()))


class ProviderCallRecordingTests(unittest.TestCase):
    """The recording has to happen where the response is parsed, and only for calls
    that actually completed."""

    def setUp(self) -> None:
        self._saved_key = os.environ.get("MISTRAL_API_KEY")
        os.environ["MISTRAL_API_KEY"] = "test-key"

    def tearDown(self) -> None:
        if self._saved_key is None:
            os.environ.pop("MISTRAL_API_KEY", None)
        else:
            os.environ["MISTRAL_API_KEY"] = self._saved_key

    def test_successful_chat_call_is_recorded_against_its_task(self) -> None:
        from procrafiler import ai_naming

        body = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 25},
        }
        original = ai_naming._post_json
        ai_naming._post_json = lambda *a, **k: (200, json.dumps(body).encode())
        try:
            with usage_scope() as usage:
                out = ai_naming.call_mistral_chat("p", "mistral-medium-latest", task="ANALYSIS")
        finally:
            ai_naming._post_json = original

        self.assertEqual(out, "ok", "the accounting must not disturb the answer")
        entry = usage.entries()[0]
        self.assertEqual(entry.task, "ANALYSIS")
        self.assertEqual(entry.model, "mistral-medium-latest")
        self.assertEqual((entry.tokens_in, entry.tokens_out), (500, 25))

    def test_rate_limited_call_is_not_recorded(self) -> None:
        """A 429 is retried. Counting the attempt would report consumption the
        user was never billed for, and inflate every run that hit a limit."""
        from procrafiler import ai_naming

        original = ai_naming._post_json
        ai_naming._post_json = lambda *a, **k: (429, b'{"object":"error","type":"rate_limited"}')
        try:
            with usage_scope() as usage:
                with self.assertRaises(ai_naming.RateLimitedError):
                    ai_naming.call_mistral_chat("p", "m", task="ANALYSIS")
        finally:
            ai_naming._post_json = original
        self.assertTrue(usage.is_empty)

    def test_ocr_call_records_pages(self) -> None:
        from procrafiler import ai_reader

        body = {"pages": [{"markdown": "text"}], "usage_info": {"pages_processed": 3}}
        original = ai_reader._post_json
        ai_reader._post_json = lambda *a, **k: (200, json.dumps(body).encode())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pdf = Path(tmp) / "scan.pdf"
                pdf.write_bytes(b"%PDF-1.4 fake")
                with usage_scope() as usage:
                    ai_reader.call_mistral_ocr(pdf, "mistral-ocr-latest")
        finally:
            ai_reader._post_json = original
        entry = usage.entries()[0]
        self.assertEqual(entry.task, "OCR")
        self.assertEqual(entry.pages, 3)


class BilledShareOfTheEstimateTests(unittest.TestCase):
    """The defect that started this: a call count shown as though it were money."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in CHAIN_VARS}
        for key in CHAIN_VARS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    @staticmethod
    def _work() -> list[tuple[str, list[Path]]]:
        return [("photos", [Path("a.jpg"), Path("b.jpg")]), ("docs", [Path("c.pdf")])]

    def test_a_fully_local_setup_is_billed_nothing(self) -> None:
        for key in CHAIN_VARS:
            os.environ[key] = "ollama:local-model"
        estimate = estimate_ai_calls(self._work())

        self.assertGreater(estimate.maximum, 0, "work is still being done…")
        self.assertEqual(estimate.billed_maximum, 0, "…but none of it is billable")
        self.assertTrue(estimate.is_free)
        self.assertIn("nothing is billed", format_estimate(estimate))

    def test_a_paid_setup_bills_every_call(self) -> None:
        for key in CHAIN_VARS:
            os.environ[key] = "mistral:paid-model"
        estimate = estimate_ai_calls(self._work())
        self.assertEqual(estimate.billed_maximum, estimate.maximum)
        self.assertFalse(estimate.is_free)
        self.assertNotIn("run locally", format_estimate(estimate))

    def test_a_mixed_setup_separates_the_two(self) -> None:
        """Vision locally, the text tasks on the API — the case where a raw call
        count is most misleading, since the images dominate it."""
        os.environ["PROCRAFILER_AI_IMAGE_PRIMARY"] = "ollama:llava"
        os.environ["PROCRAFILER_AI_OCR_PRIMARY"] = "ollama:llava"
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:medium"
        os.environ["PROCRAFILER_AI_NAMING_PRIMARY"] = "mistral:medium"
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:medium"

        estimate = estimate_ai_calls(self._work())
        self.assertGreater(estimate.billed_maximum, 0)
        self.assertLess(estimate.billed_maximum, estimate.maximum)
        self.assertEqual(estimate.billed_minimum, estimate.billed_maximum, "text tasks are a fixed count")
        line = format_estimate(estimate)
        self.assertIn("are billed", line)
        self.assertIn("run locally", line)

    def test_a_paid_fallback_behind_a_local_primary_is_not_quoted(self) -> None:
        """Quoting a run at the price of its worst case would warn about money on
        every single local run. The fallback is reported by measurement instead."""
        for key in CHAIN_VARS:
            os.environ[key] = "ollama:local-model"
        os.environ["PROCRAFILER_AI_ANALYSIS_FALLBACK"] = "mistral:medium"
        try:
            self.assertTrue(estimate_ai_calls(self._work()).is_free)
        finally:
            os.environ.pop("PROCRAFILER_AI_ANALYSIS_FALLBACK", None)


class PersistenceTests(unittest.TestCase):
    """Printed lines die with the terminal. What a run consumed has to survive it,
    or calibrating a future forecast on real measurements is impossible."""

    def setUp(self) -> None:
        from procrafiler.config import default_runtime_paths, ensure_runtime_layout

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "PROCRAFILER_WORKSPACE_DIR",
                "PROCRAFILER_LIBRARY_DIR",
                "PROCRAFILER_LIBRARY_MIRROR_DIR",
                "PROCRAFILER_HOME",
                "PROCRAFILER_CONFIG_HOME",
            )
        }
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def _logged_usage_events(self) -> list[dict]:
        log = self.paths.actions_log_file
        if not log.is_file():
            return []
        events = [json.loads(line) for line in log.read_text("utf-8").splitlines() if line.strip()]
        return [e for e in events if e.get("action") == "run_ai_usage"]

    def test_the_breakdown_is_written_to_the_action_log(self) -> None:
        from procrafiler.pipeline import _report_run_usage

        usage = RunUsage()
        usage.add(provider="mistral", model="medium", task="ANALYSIS", tokens_in=900, tokens_out=40)
        usage.add(provider="ollama", model="llava", task="IMAGE", tokens_in=5000, tokens_out=10)

        lines: list[str] = []
        _report_run_usage(self.paths, usage, progress=lines.append, now_utc=None)

        events = self._logged_usage_events()
        self.assertEqual(len(events), 1)
        breakdown = events[0]["ai_usage"]
        self.assertEqual(len(breakdown), 2, "one record per provider/model/task, kept apart")
        by_task = {row["task"]: row for row in breakdown}
        self.assertTrue(by_task["ANALYSIS"]["billed"])
        self.assertFalse(by_task["IMAGE"]["billed"], "a local call must not be persisted as billable")
        self.assertEqual(by_task["ANALYSIS"]["tokens_in"], 900)
        self.assertTrue(lines, "the user is also told, not only the log")

    def test_a_run_with_no_ai_call_writes_nothing(self) -> None:
        """A dry run makes no call. An event saying "0 tokens" on every one of them
        would bury the real ones."""
        from procrafiler.pipeline import _report_run_usage

        lines: list[str] = []
        _report_run_usage(self.paths, RunUsage(), progress=lines.append, now_utc=None)
        self.assertEqual(self._logged_usage_events(), [])
        self.assertEqual(lines, [])

    def test_an_interrupted_run_still_reports_what_it_spent(self) -> None:
        """The calls already made are billed whether or not the batch finished —
        which is exactly when the user most wants the number."""
        from procrafiler import pipeline

        def _explode(paths, now_utc, dry_run, progress, limit, confirm=None):  # noqa: ANN001
            record_response("mistral", "medium", "ANALYSIS", {"usage": {"prompt_tokens": 700, "completion_tokens": 30}})
            raise KeyboardInterrupt("user pressed ctrl-c")

        original = pipeline._process_all_inbox_files
        pipeline._process_all_inbox_files = _explode
        try:
            with self.assertRaises(KeyboardInterrupt):
                pipeline.process_all_inbox_files(self.paths, progress=lambda _m: None)
        finally:
            pipeline._process_all_inbox_files = original

        events = self._logged_usage_events()
        self.assertEqual(len(events), 1, "the interrupted run still accounted for its calls")
        self.assertEqual(events[0]["ai_usage"][0]["tokens_in"], 700)


if __name__ == "__main__":
    unittest.main()
