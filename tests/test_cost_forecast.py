# pyright: reportUnknownVariableType=false
"""Knowing what a run will cost in money, BEFORE it spends any.

Measuring what a finished run consumed answers the wrong half of the question: by
then the money is gone. What the user asked for is a figure beforehand — coarse on
a fresh install, calibrated on their own history afterwards — and a ceiling that
stops a batch of sixty photos rather than reporting it after the fact.

Two things these tests defend above all. A price the table does not know must never
be treated as zero, because that turns "I cannot tell you" into "it is free". And
the ceiling must trigger on the UPPER bound of the estimate, since a guard that
only fires at the optimistic end lets through exactly the run it exists to catch.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from procrafiler.ai_estimate import estimate_ai_calls
from procrafiler.cost_forecast import (
    CostForecast,
    DEFAULT_PROFILES,
    TaskProfile,
    forecast_cost,
    format_cost_forecast,
    max_run_cost,
    profiles_from_history,
)
from procrafiler.pricing import (
    ModelPrice,
    PriceTable,
    ProviderPrices,
    format_amount,
    load_price_table,
)

CHAIN_VARS = (
    "PROCRAFILER_AI_IMAGE_PRIMARY",
    "PROCRAFILER_AI_OCR_PRIMARY",
    "PROCRAFILER_AI_ANALYSIS_PRIMARY",
    "PROCRAFILER_AI_NAMING_PRIMARY",
    "PROCRAFILER_AI_ORGANIZE_PRIMARY",
)
PAID = {
    "PROCRAFILER_AI_IMAGE_PRIMARY": "mistral:mistral-medium-latest",
    "PROCRAFILER_AI_OCR_PRIMARY": "mistral:mistral-ocr-latest",
    "PROCRAFILER_AI_ANALYSIS_PRIMARY": "mistral:mistral-medium-latest",
    "PROCRAFILER_AI_NAMING_PRIMARY": "mistral:mistral-medium-latest",
    "PROCRAFILER_AI_ORGANIZE_PRIMARY": "mistral:mistral-medium-latest",
}


class _EnvIsolated(unittest.TestCase):
    _KEYS: tuple[str, ...] = CHAIN_VARS + ("PROCRAFILER_MAX_RUN_COST",)

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        for key in self._KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


class ShippedTableTests(unittest.TestCase):
    def test_the_packaged_table_covers_every_model_the_app_uses(self) -> None:
        """A model the app configures by default but the table ignores would make
        every forecast silently incomplete."""
        table = load_price_table()
        self.assertIsNotNone(table)
        assert table is not None
        for model in ("mistral-medium-latest", "mistral-small-latest", "mistral-ocr-latest"):
            with self.subTest(model=model):
                price = table.price_for("mistral", model)
                self.assertIsNotNone(price, f"{model} missing from the shipped table")
                assert price is not None
                self.assertTrue(price.is_priceable)
        self.assertNotEqual(
            table.as_of(), "unknown date", "a table with no date cannot be displayed honestly"
        )

    def test_the_shipped_table_carries_every_seller_the_source_publishes(self) -> None:
        """Sellers ProcraFiler cannot call **yet** are shipped on purpose. The
        `ai_*` modules speak to Mistral and Ollama today, and the others are the
        work ahead; a table that only carried today's provider would have to be
        re-cut for each one, and an offline machine would have no figure at all on
        the day the code caught up.

        Priced per seller, so carrying them costs nothing but bytes: nothing asks
        the whole table a question any more (see `test_pricing_scope`).
        """
        table = load_price_table()
        assert table is not None
        self.assertGreaterEqual(len(table.providers), 2)
        for name, seller in table.providers.items():
            with self.subTest(seller=name):
                self.assertTrue(
                    any(price.is_priceable for price in seller.models.values()),
                    f"{name} ships with no usable price at all",
                )
                self.assertNotEqual(seller.updated, "", f"{name} ships undated")

    def test_every_seller_in_the_shipped_table_keeps_its_own_currency(self) -> None:
        """The whole reason the file is keyed by provider: Mistral publishes in USD
        and OVH in EUR. A single top-level currency cannot be true for both, and
        applying the wrong one silently changes what every figure means."""
        table = load_price_table()
        assert table is not None
        self.assertEqual(table.currency_of("mistral"), "USD")
        if "ovh" in table.providers:
            self.assertEqual(table.currency_of("ovh"), "EUR")

    def test_a_model_of_one_seller_is_not_priced_for_another(self) -> None:
        table = load_price_table()
        assert table is not None
        if "ovh" not in table.providers:
            self.skipTest("the shipped table has a single provider")
        ovh_only = next(iter(table.providers["ovh"].models))
        self.assertIsNone(table.price_for("mistral", ovh_only))

    def test_a_free_model_costs_zero_and_an_unknown_one_costs_nothing_known(self) -> None:
        """The one legitimate zero. "This is free" is a fact the provider declares;
        "I have no price for this" is an admission — and the whole point of this
        module is that the two never look alike."""
        table = load_price_table()
        assert table is not None
        free = [
            name for name, price in table.providers.get("ovh", ProviderPrices()).models.items()
            if price.free
        ]
        if not free:
            self.skipTest("no free model in the shipped table")
        self.assertEqual(table.cost("ovh", free[0], tokens_in=10_000_000), 0.0)
        self.assertIsNone(table.cost("ovh", "no-such-model", tokens_in=10_000_000))

    def test_a_user_file_overrides_the_shipped_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            (config / "pricing.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated": "2030-01-01",
                        "currency": "EUR",
                        "currency_symbol": "€",
                        "models": {"mistral-medium-latest": {"in_per_mtok": 99.0, "out_per_mtok": 1.0}},
                    }
                ),
                "utf-8",
            )
            table = load_price_table(config)
        assert table is not None
        # A schema-1 file names no seller, so it lands in the `*` bucket and must
        # still answer for whichever provider the run actually uses.
        self.assertEqual(table.currency_of("mistral"), "EUR")
        price = table.price_for("mistral", "mistral-medium-latest")
        assert price is not None
        self.assertEqual(price.in_per_mtok, 99.0)

    def test_a_newer_schema_falls_back_instead_of_being_misread(self) -> None:
        """A field that changed meaning would be applied to real money."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            (config / "pricing.json").write_text(
                json.dumps({"schema_version": 99, "models": {"x": {"in_per_mtok": 1.0}}}), "utf-8"
            )
            table = load_price_table(config)
        assert table is not None
        self.assertEqual(table.origin, "shipped with the app")


class PriceArithmeticTests(unittest.TestCase):
    TABLE = PriceTable(
        providers={
            "mistral": ProviderPrices(
                currency="USD", currency_symbol="$", updated="2026-07-30",
                models={
                    "chat": ModelPrice(in_per_mtok=1.5, out_per_mtok=7.5),
                    "ocr": ModelPrice(per_1k_pages=4.0),
                },
            )
        }
    )

    def test_tokens_are_priced_per_million_each_way(self) -> None:
        cost = self.TABLE.cost("mistral", "chat", tokens_in=1_000_000, tokens_out=1_000_000)
        self.assertAlmostEqual(cost or 0.0, 9.0)

    def test_pages_are_priced_per_thousand(self) -> None:
        self.assertAlmostEqual(self.TABLE.cost("mistral", "ocr", pages=500) or 0.0, 2.0)

    def test_only_the_units_the_table_prices_are_charged(self) -> None:
        """Voxtral Mini bills per minute of audio, and its reply ALSO reports token
        counts that it does not charge for. The table is what decides: no token
        price in its entry, so tokens cost nothing — an hour of speech reports
        hundreds of thousands of them, so getting this wrong would not be a small
        error."""
        from procrafiler.pricing import load_price_table

        table = load_price_table()
        assert table is not None
        audio_only = table.cost("mistral", "voxtral-mini-latest", audio_seconds=600)
        with_tokens = table.cost("mistral", "voxtral-mini-latest", audio_seconds=600, tokens_in=5_000_000)
        self.assertEqual(audio_only, with_tokens, "a unit with no price in the table is free")
        self.assertAlmostEqual(audio_only or 0.0, 600 / 60 * 0.003)

    def test_a_model_that_bills_both_ways_is_charged_both_ways(self) -> None:
        """Some models charge per audio minute AND per text token — Voxtral Small
        is one, at the rates used here. Suppressing the tokens because a duration
        price exists would under-report the bill.

        Built here rather than read from the shipped table: that model is one
        ProcraFiler never configures, so it is deliberately absent from
        `price_labels.json` and has no price. The shape is what this test is about,
        not the seller's current figure for it."""
        table = PriceTable(
            providers={
                "mistral": ProviderPrices(
                    models={"two-units": ModelPrice(in_per_mtok=0.1, per_audio_minute=0.004)}
                )
            }
        )
        both = table.cost("mistral", "two-units", audio_seconds=600, tokens_in=1_000_000)
        audio_part = 600 / 60 * 0.004
        self.assertGreater(both or 0.0, audio_part, "the token price must be added too")
        self.assertAlmostEqual(both or 0.0, audio_part + 0.1)

    def test_an_unknown_model_has_no_price_rather_than_a_free_one(self) -> None:
        self.assertIsNone(self.TABLE.cost("mistral", "who-knows", tokens_in=10_000_000))

    def test_an_alias_is_never_matched_by_prefix(self) -> None:
        """`mistral-medium-latest` will one day point at another model at another
        price; guessing from a prefix would price something we have never seen."""
        self.assertIsNone(self.TABLE.price_for("mistral", "chat-v2"))

    def test_absurd_or_malformed_prices_are_rejected(self) -> None:
        from procrafiler.pricing import _parse_table

        table = _parse_table(
            {
                "schema_version": 1,
                "models": {
                    "a": {"in_per_mtok": -1.0},
                    "b": {"in_per_mtok": "1.5"},
                    "c": {"in_per_mtok": 999_999.0},
                    "d": {"in_per_mtok": 2.0},
                },
            },
            "test",
        )
        assert table is not None
        for model in ("a", "b", "c"):
            with self.subTest(model=model):
                price = table.price_for("mistral", model)
                assert price is not None
                self.assertFalse(price.is_priceable, f"{model} should have been rejected")
        good = table.price_for("mistral", "d")
        assert good is not None
        self.assertEqual(good.in_per_mtok, 2.0)

    def test_a_real_charge_is_never_displayed_as_zero(self) -> None:
        """Most runs are cents. Rounding to two decimals would print $0.00 for a
        charge that is about to happen."""
        self.assertEqual(format_amount(0.004, "$"), "<$0.01")
        self.assertEqual(format_amount(0.0, "$"), "$0.00")
        self.assertEqual(format_amount(1.239, "$"), "$1.24")

    def test_staleness_is_measured_from_the_table_date(self) -> None:
        self.assertFalse(self.TABLE.is_stale(date(2026, 8, 30)))
        self.assertTrue(self.TABLE.is_stale(date(2027, 8, 30)))


class ForecastTests(_EnvIsolated):
    @staticmethod
    def _work(photos: int = 3, pdfs: int = 1) -> list[tuple[str, list[Path]]]:
        return [
            ("photos", [Path(f"IMG_{i}.jpg") for i in range(photos)]),
            ("docs", [Path(f"doc_{i}.pdf") for i in range(pdfs)]),
        ]

    def test_a_fully_local_run_costs_nothing(self) -> None:
        for key in CHAIN_VARS:
            os.environ[key] = "ollama:llava"
        forecast = forecast_cost(estimate_ai_calls(self._work()))
        assert forecast is not None
        self.assertTrue(forecast.is_free)
        self.assertEqual(forecast.high, 0.0)
        self.assertIn("nothing", format_cost_forecast(forecast))

    def test_a_paid_run_is_priced_and_dated(self) -> None:
        os.environ.update(PAID)
        forecast = forecast_cost(estimate_ai_calls(self._work()))
        assert forecast is not None
        self.assertGreater(forecast.high, 0.0)
        self.assertGreater(forecast.high, forecast.low, "the OCR maybe-call is the range")
        line = format_cost_forecast(forecast)
        self.assertIn("rates of", line, "a price without its date is a claim we cannot support")

    def test_the_displayed_currency_is_the_SELLERS_not_a_default(self) -> None:
        """A forecast that forgot which currency it priced in would fall back to
        USD and print dollars for a euro bill — the same figure, silently meaning
        something else."""
        for key in CHAIN_VARS:
            os.environ.pop(key, None)
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "ovh:chat"
        table = PriceTable(
            providers={
                "ovh": ProviderPrices(
                    currency="EUR", currency_symbol="\u20ac", updated="2026-08-04",
                    models={"chat": ModelPrice(in_per_mtok=1.0, out_per_mtok=2.0)},
                )
            }
        )
        forecast = forecast_cost(estimate_ai_calls(self._work()), table=table)
        assert forecast is not None
        self.assertEqual(forecast.currencies, frozenset({"EUR"}))
        line = format_cost_forecast(forecast)
        self.assertIn("EUR", line)
        self.assertIn("\u20ac", line)
        self.assertNotIn("USD", line)

    def test_more_photos_cost_more(self) -> None:
        os.environ.update(PAID)
        small = forecast_cost(estimate_ai_calls(self._work(photos=3)))
        large = forecast_cost(estimate_ai_calls(self._work(photos=60)))
        assert small is not None and large is not None
        self.assertGreater(large.high, small.high * 5)

    def test_a_billable_model_with_no_price_is_flagged_not_ignored(self) -> None:
        """The failure that would actively mislead: reporting $0.00 for a run whose
        model we cannot price."""
        os.environ.update(PAID)
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:some-unreleased-model"
        forecast = forecast_cost(estimate_ai_calls(self._work()))
        assert forecast is not None
        self.assertFalse(forecast.is_complete)
        self.assertIn("some-unreleased-model", forecast.unpriced_models)
        self.assertIn("AT LEAST", format_cost_forecast(forecast))

    def test_a_local_model_absent_from_the_table_is_not_an_error(self) -> None:
        """Ollama models are missing from the table by design — they are free, not
        unpriceable, and must not raise the "no price known" alarm."""
        os.environ.update(PAID)
        os.environ["PROCRAFILER_AI_IMAGE_PRIMARY"] = "ollama:llava"
        forecast = forecast_cost(estimate_ai_calls(self._work()))
        assert forecast is not None
        self.assertTrue(forecast.is_complete)

    def test_the_coarse_tasks_are_admitted_in_the_output(self) -> None:
        os.environ.update(PAID)
        forecast = forecast_cost(estimate_ai_calls(self._work()))
        assert forecast is not None
        self.assertIn("IMAGE", forecast.coarse_tasks)
        self.assertIn("rough default", format_cost_forecast(forecast))

    def test_history_replaces_the_default_and_says_so(self) -> None:
        os.environ.update(PAID)
        work = self._work(photos=10, pdfs=0)
        default = forecast_cost(estimate_ai_calls(work))
        # This user's photos turn out to be four times heavier than the seed.
        heavy = {"IMAGE": TaskProfile(tokens_in=DEFAULT_PROFILES["IMAGE"].tokens_in * 4, tokens_out=350)}
        calibrated = forecast_cost(estimate_ai_calls(work), profiles=heavy)
        assert default is not None and calibrated is not None
        self.assertGreater(calibrated.high, default.high)
        self.assertIn("IMAGE", calibrated.calibrated_tasks)
        self.assertNotIn("IMAGE", calibrated.coarse_tasks, "a measured task is no longer a guess")
        self.assertIn("calibrated on your previous runs", format_cost_forecast(calibrated))


class HistoryTests(unittest.TestCase):
    def _log(self, tmp: str, rows: list[dict]) -> object:
        log = Path(tmp) / "actions.jsonl"
        log.write_text(
            "\n".join(json.dumps({"action": "run_ai_usage", "ai_usage": [row]}) for row in rows),
            "utf-8",
        )

        class _Paths:
            actions_log_file = log

        return _Paths()

    def test_averages_are_per_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._log(tmp, [{"task": "IMAGE", "calls": 10, "tokens_in": 50_000, "tokens_out": 2_000}])
            profiles = profiles_from_history(paths)
        self.assertEqual(profiles["IMAGE"].tokens_in, 5_000)
        self.assertEqual(profiles["IMAGE"].tokens_out, 200)

    def test_a_thin_history_is_not_trusted(self) -> None:
        """One odd document must not become the model for every future run."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._log(tmp, [{"task": "IMAGE", "calls": 2, "tokens_in": 999_999}])
            self.assertEqual(profiles_from_history(paths), {})

    def test_calls_whose_volume_was_never_reported_do_not_dilute_the_average(self) -> None:
        """Counting them would divide the real total by a larger number of calls and
        quietly halve every forecast."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._log(
                tmp,
                [{"task": "IMAGE", "calls": 10, "unmeasured_calls": 5, "tokens_in": 25_000}],
            )
            profiles = profiles_from_history(paths)
        self.assertEqual(profiles["IMAGE"].tokens_in, 5_000, "25000 over the 5 measured calls")

    def test_a_truncated_log_line_does_not_hide_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "actions.jsonl"
            log.write_text(
                '{"action": "run_ai_usage", "ai_usage": [{"task": "OCR", "calls": 8, "pages": 40}]}\n'
                '{"action": "run_ai_usage", "ai_us',
                "utf-8",
            )

            class _Paths:
                actions_log_file = log

            profiles = profiles_from_history(_Paths())
        self.assertEqual(profiles["OCR"].pages, 5)

    def test_no_log_at_all_is_not_an_error(self) -> None:
        class _Paths:
            actions_log_file = Path("/nonexistent/actions.jsonl")

        self.assertEqual(profiles_from_history(_Paths()), {})


class CeilingTests(_EnvIsolated):
    def test_unset_means_no_ceiling(self) -> None:
        self.assertIsNone(max_run_cost())

    def test_a_comma_decimal_is_accepted(self) -> None:
        os.environ["PROCRAFILER_MAX_RUN_COST"] = "5,50"
        self.assertEqual(max_run_cost(), 5.5)

    def test_nonsense_is_ignored_rather_than_becoming_a_ceiling_of_zero(self) -> None:
        """A ceiling of zero would block every run; the safe reading of a typo is
        "no ceiling configured"."""
        for raw in ("abc", "-3", "0"):
            with self.subTest(raw=raw):
                os.environ["PROCRAFILER_MAX_RUN_COST"] = raw
                self.assertIsNone(max_run_cost())

    def test_it_triggers_on_the_upper_bound(self) -> None:
        """A guard checking only the optimistic end would wave through the run it
        exists to stop."""
        from procrafiler.pipeline import _cost_ceiling_accepted
        from procrafiler.pricing import PriceTable
        from procrafiler.cost_forecast import CostForecast

        table = PriceTable(providers={"mistral": ProviderPrices(updated="2026-07-30")})
        forecast = CostForecast(table=table, low=0.5, high=9.0, billed_calls_high=100)
        os.environ["PROCRAFILER_MAX_RUN_COST"] = "5"

        asked: list[str] = []

        def _decline(question: str) -> bool:
            asked.append(question)
            return False

        self.assertFalse(_cost_ceiling_accepted(forecast, confirm=_decline, emit=lambda _m: None))
        self.assertTrue(asked, "the low bound was under the ceiling, the high bound was not")
        self.assertIn("9.00", asked[0])

    def test_accepting_lets_the_run_through(self) -> None:
        from procrafiler.pipeline import _cost_ceiling_accepted
        from procrafiler.cost_forecast import CostForecast

        forecast = CostForecast(table=PriceTable(), low=9.0, high=9.0)
        os.environ["PROCRAFILER_MAX_RUN_COST"] = "5"
        self.assertTrue(_cost_ceiling_accepted(forecast, confirm=lambda _q: True, emit=lambda _m: None))

    def test_under_the_ceiling_nobody_is_asked(self) -> None:
        """Crying wolf on every run trains the user to type y without reading."""
        from procrafiler.pipeline import _cost_ceiling_accepted
        from procrafiler.cost_forecast import CostForecast

        forecast = CostForecast(table=PriceTable(), low=0.1, high=0.2)
        os.environ["PROCRAFILER_MAX_RUN_COST"] = "5"

        def _explode(_question: str) -> bool:
            raise AssertionError("must not ask")

        self.assertTrue(_cost_ceiling_accepted(forecast, confirm=_explode, emit=lambda _m: None))

    def test_a_non_interactive_caller_is_not_blocked(self) -> None:
        """No way to ask means no way to answer; a cron job must not hang forever."""
        from procrafiler.pipeline import _cost_ceiling_accepted
        from procrafiler.cost_forecast import CostForecast

        forecast = CostForecast(table=PriceTable(), low=9.0, high=9.0)
        os.environ["PROCRAFILER_MAX_RUN_COST"] = "5"
        warned: list[str] = []
        self.assertTrue(_cost_ceiling_accepted(forecast, confirm=None, emit=warned.append))
        self.assertTrue(any("⚠" in line for line in warned))


class SpendPromptTests(unittest.TestCase):
    """The terminal answer to the ceiling question. It authorises a charge, so its
    default must be refusal — a stray Enter is not consent."""

    def _answer(self, reply):  # noqa: ANN001
        import builtins

        from procrafiler.cli import _confirm_spend

        original = builtins.input
        builtins.input = (lambda _p="": (_ for _ in ()).throw(reply)) if isinstance(reply, type) else (lambda _p="": reply)
        try:
            return _confirm_spend("Spend?")
        finally:
            builtins.input = original

    def test_only_an_explicit_yes_authorises_the_charge(self) -> None:
        for reply in ("y", "Y", "yes", "  YES  "):
            with self.subTest(reply=reply):
                self.assertTrue(self._answer(reply))

    def test_anything_else_refuses(self) -> None:
        for reply in ("", " ", "n", "no", "oui", "ok", "sure", "yeah"):
            with self.subTest(reply=reply):
                self.assertFalse(self._answer(reply), f"{reply!r} must not authorise spending")

    def test_a_closed_stdin_refuses_rather_than_crashing(self) -> None:
        self.assertFalse(self._answer(EOFError))


class CeilingStopsTheRunTests(_EnvIsolated):
    """The ceiling is only worth anything if declining it actually prevents the
    spending — before the first call, not after."""

    _KEYS = CHAIN_VARS + (
        "PROCRAFILER_MAX_RUN_COST",
        "PROCRAFILER_WORKSPACE_DIR",
        "PROCRAFILER_LIBRARY_DIR",
        "PROCRAFILER_LIBRARY_MIRROR_DIR",
        "PROCRAFILER_HOME",
        "PROCRAFILER_CONFIG_HOME",
    )

    def setUp(self) -> None:
        super().setUp()
        from procrafiler.config import default_runtime_paths, ensure_runtime_layout

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ.update(PAID)
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        for index in range(6):
            (self.paths.inbox_dir / f"IMG_{index}.jpg").write_bytes(b"fake image bytes")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def test_declining_stops_before_any_file_is_touched(self) -> None:
        from procrafiler.pipeline import process_all_inbox_files

        os.environ["PROCRAFILER_MAX_RUN_COST"] = "0.01"  # any real run exceeds this
        asked: list[str] = []
        summary = process_all_inbox_files(
            self.paths,
            progress=lambda _m: None,
            confirm=lambda question: (asked.append(question), False)[1],
        )

        self.assertTrue(asked, "the user was asked before spending")
        self.assertEqual(summary.get("aborted"), 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(
            len(list(self.paths.inbox_dir.glob("*.jpg"))), 6, "the Inbox was left exactly as it was"
        )
        self.assertFalse(
            any(self.paths.queue_dir.iterdir()), "nothing was staged into the Queue either"
        )

    def test_a_generous_ceiling_does_not_ask(self) -> None:
        from procrafiler.pipeline import process_all_inbox_files

        os.environ["PROCRAFILER_MAX_RUN_COST"] = "1000"

        def _explode(_question: str) -> bool:
            raise AssertionError("a run well under the ceiling must not interrupt the user")

        summary = process_all_inbox_files(
            self.paths, progress=lambda _m: None, confirm=_explode, dry_run=True
        )
        self.assertEqual(summary.get("aborted", 0), 0)


if __name__ == "__main__":
    unittest.main()


class MixedCurrencyTests(unittest.TestCase):
    """Two sellers, two currencies, and no exchange rate anywhere in this app.

    It cannot happen today — the only billed provider the app can call is Mistral —
    but the price table already describes sellers who publish in different money,
    and the moment a second one is callable a run can price one task in dollars and
    another in euros. Adding those together is not arithmetic.
    """

    TABLE = PriceTable(
        providers={
            "mistral": ProviderPrices(
                currency="USD", currency_symbol="$", updated="2026-08-04",
                models={"chat": ModelPrice(in_per_mtok=1.0)},
            ),
            "ovh": ProviderPrices(
                currency="EUR", currency_symbol="\u20ac", updated="2026-08-04",
                models={"chat": ModelPrice(in_per_mtok=1.0)},
            ),
        }
    )

    def test_one_currency_is_shown_normally(self) -> None:
        forecast = CostForecast(
            table=self.TABLE, low=1.0, high=2.0, billed_calls_high=4,
            currencies=frozenset({"USD"}), currency_symbol="$",
        )
        line = format_cost_forecast(forecast)
        self.assertIn("$1.00 to $2.00", line)
        self.assertIn("USD", line)

    def test_two_currencies_refuse_to_produce_a_total(self) -> None:
        """Naming both and declining beats printing a number that means nothing."""
        forecast = CostForecast(
            table=self.TABLE, low=1.0, high=2.0, billed_calls_high=4,
            currencies=frozenset({"USD", "EUR"}), currency_symbol="$",
        )
        line = format_cost_forecast(forecast)
        self.assertIn("not summable", line)
        self.assertIn("EUR", line)
        self.assertIn("USD", line)
        self.assertNotIn("2.00", line, "a total was printed across two currencies")

    def test_the_symbol_follows_the_seller(self) -> None:
        self.assertEqual(self.TABLE.symbol_of("mistral"), "$")
        self.assertEqual(self.TABLE.symbol_of("ovh"), "\u20ac")

    def test_an_unknown_currency_is_shown_by_its_code_not_a_guessed_symbol(self) -> None:
        from procrafiler.pricing import _parse_table

        table = _parse_table(
            {
                "schema_version": 2,
                "providers": {
                    "somebody": {
                        "currency": "CHF", "updated": "2026-08-04",
                        "models": {"m": {"in_per_mtok": 1.0}},
                    }
                },
            },
            "test",
        )
        assert table is not None
        self.assertIn("CHF", table.symbol_of("somebody"))


class ProviderUnitTests(unittest.TestCase):
    """Units and dates that only appear once there is more than one seller."""

    TABLE = PriceTable(
        providers={
            "mistral": ProviderPrices(
                currency="USD", currency_symbol="$",
                updated="2026-08-04", checked_utc="2026-08-04T00:00:00Z",
                models={"voxtral": ModelPrice(per_audio_minute=0.003)},
            ),
            "ovh": ProviderPrices(
                currency="EUR", currency_symbol="\u20ac",
                updated="2025-01-15", checked_utc="2025-01-15T00:00:00Z",
                models={
                    "whisper": ModelPrice(per_audio_second=1.278e-05),
                    "gratuit": ModelPrice(free=True),
                    # A contradictory entry: declared free AND carrying a price.
                    "gratuit-mais-tarifé": ModelPrice(free=True, in_per_mtok=5.0),
                },
            ),
        }
    )

    def test_audio_priced_by_the_second_is_charged_by_the_second(self) -> None:
        """OVH prices Whisper per second where Mistral prices Voxtral per minute.
        Both are kept as published, so a figure can be checked against its source
        page; treating one as the other is a factor-of-sixty error."""
        self.assertAlmostEqual(
            self.TABLE.cost("ovh", "whisper", audio_seconds=600) or 0.0, 600 * 1.278e-05
        )

    def test_the_two_audio_units_do_not_price_the_same(self) -> None:
        per_minute = self.TABLE.cost("mistral", "voxtral", audio_seconds=600)
        per_second = self.TABLE.cost("ovh", "whisper", audio_seconds=600)
        self.assertNotAlmostEqual(per_minute or 0.0, per_second or 0.0)

    def test_free_wins_over_any_price_in_the_same_entry(self) -> None:
        """A published file that says both is wrong somewhere. Charging is the
        expensive way to be wrong, so `free` decides."""
        self.assertEqual(
            self.TABLE.cost("ovh", "gratuit-mais-tarifé", tokens_in=10_000_000), 0.0
        )

    def test_a_free_model_costs_zero_whatever_it_consumed(self) -> None:
        self.assertEqual(self.TABLE.cost("ovh", "gratuit", tokens_in=10_000_000, pages=500), 0.0)

    def test_the_displayed_date_is_the_OLDEST_sellers(self) -> None:
        """A table is only as trustworthy as its stalest part. Showing the freshest
        date would let a Mistral check made today vouch for an OVH page nobody has
        looked at since last year."""
        self.assertEqual(self.TABLE.as_of(), "2025-01-15")

    def test_staleness_is_measured_on_the_OLDEST_seller_too(self) -> None:
        self.assertEqual(self.TABLE.age_days(date(2025, 1, 20)), 5, "measured from the oldest")
        self.assertTrue(self.TABLE.is_stale(date(2026, 8, 4)), "the OVH figures are 18 months old")
