# pyright: reportUnknownVariableType=false
"""The price table sells to more people than us.

The companion repository was written for ProcraFiler and now publishes four
sellers — Mistral, OVH, EdenAI, HuggingFace — with more to come. Not one field
changed; the file simply grew. That alone broke two things quietly, because both
asked their question of the WHOLE table when the only answer that means anything
is about the sellers this installation actually buys from.

**The download guard.** It refused a file in which no model anywhere was
priceable. Measured against the live file: mangle every Mistral figure and a
hundred healthy EdenAI models answer for them, so the download is accepted and
`mistral-small-latest` ends up with no price at all — the exact substitution of a
useless table for a working one the guard exists to prevent.

**The date beside the figure.** `as_of` and `age_days` report the OLDEST seller,
deliberately: a table is only as trustworthy as its stalest part. Across sellers
we cannot even call, that becomes a lie in both directions — an unmaintained
EdenAI block would date, and eventually declare stale, a forecast computed
entirely from Mistral rates checked yesterday.

The staleness path also hid an `AttributeError` waiting for its first old table:
`table.source` survived the move of `source` onto each seller, and nothing had yet
aged past the threshold to find out.
"""
from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone

from procrafiler.ai_estimate import AICallEstimate
from procrafiler.ai_naming import SUPPORTED_AI_TASKS, configured_providers
from procrafiler.cost_forecast import CostForecast, forecast_cost, format_cost_forecast
from procrafiler.pricing import ModelPrice, PriceTable, ProviderPrices
from procrafiler.pricing_refresh import _prices_what_we_buy  # type: ignore[reportPrivateUsage]

_TODAY = datetime.now(timezone.utc).date().isoformat()


def _seller(
    *,
    updated: str = "2026-08-04",
    source: str = "",
    priceable: bool = True,
    currency: str = "USD",
    model: str = "chat",
) -> ProviderPrices:
    """One seller's block. `priceable=False` is what a mangled one looks like AFTER
    parsing: the models are still there, every figure stripped to None by the
    bounds check."""
    price = ModelPrice(in_per_mtok=1.0) if priceable else ModelPrice()
    return ProviderPrices(
        currency=currency, updated=updated, source=source, models={model: price}
    )


class _EnvIsolated(unittest.TestCase):
    """Every provider chain, cleared. `configured_providers` reads all of them."""

    def setUp(self) -> None:
        self._keys = [
            f"PROCRAFILER_AI_{task}_{slot}"
            for task in SUPPORTED_AI_TASKS
            for slot in ("PRIMARY", "FALLBACK")
        ]
        self._saved = {k: os.environ.get(k) for k in self._keys}
        for key in self._keys:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


class ConfiguredProvidersTests(_EnvIsolated):
    def test_it_reports_who_we_buy_from(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.assertEqual(configured_providers(), frozenset({"mistral"}))

    def test_a_fallback_counts_as_much_as_a_primary(self) -> None:
        """It is billed exactly like a primary the day it fires."""
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "ollama:qwen3.5:9b"
        os.environ["PROCRAFILER_AI_ANALYSIS_FALLBACK"] = "mistral:mistral-small-latest"
        self.assertEqual(configured_providers(), frozenset({"ollama", "mistral"}))

    def test_several_tasks_several_sellers(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"] = "ovh:whisper-large-v3-turbo"
        self.assertEqual(configured_providers(), frozenset({"mistral", "ovh"}))

    def test_an_unconfigured_installation_names_nobody(self) -> None:
        self.assertEqual(configured_providers(), frozenset())

    def test_a_malformed_chain_contributes_nothing(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "no-colon-here"
        self.assertEqual(configured_providers(), frozenset())


class DownloadGuardTests(_EnvIsolated):
    """Whether a downloaded file may replace the one in force."""

    def setUp(self) -> None:
        super().setUp()
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"

    def test_our_sellers_block_mangled_is_refused_however_healthy_the_others(self) -> None:
        """The headline. Reproduced from the live file: with Mistral's figures all
        out of bounds, the old check passed on the strength of EdenAI's."""
        table = PriceTable(providers={
            "mistral": _seller(priceable=False),
            "edenai": _seller(),
            "huggingface": _seller(),
        })
        self.assertFalse(_prices_what_we_buy(table))

    def test_a_stranger_being_broken_does_not_refuse_the_file(self) -> None:
        """Anti-vacuity, and the reason the check is scoped rather than stricter: a
        seller we never call is none of our business."""
        table = PriceTable(providers={
            "mistral": _seller(),
            "edenai": _seller(priceable=False),
        })
        self.assertTrue(_prices_what_we_buy(table))

    def test_a_healthy_file_is_accepted(self) -> None:
        self.assertTrue(_prices_what_we_buy(PriceTable(providers={"mistral": _seller()})))

    def test_our_seller_missing_altogether_is_refused(self) -> None:
        """A file that dropped the only block we read is no more usable than one
        that mangled it."""
        table = PriceTable(providers={"edenai": _seller(), "huggingface": _seller()})
        self.assertFalse(_prices_what_we_buy(table))

    def test_one_priceable_model_is_enough(self) -> None:
        """Deliberately not per MODEL: someone who pins `mistral-small-2506`, or
        picks a model the companion repo does not list, must not have every future
        refresh refused for ever, silently."""
        table = PriceTable(providers={"mistral": ProviderPrices(
            updated="2026-08-04",
            models={"other-model": ModelPrice(in_per_mtok=1.0), "mistral-small-latest": ModelPrice()},
        )})
        self.assertTrue(_prices_what_we_buy(table))

    def test_a_seller_less_file_still_answers_for_a_named_seller(self) -> None:
        """A schema-1 file, or the user's own hand-written rate, lands in the `*`
        bucket and applies whoever serves it."""
        table = PriceTable(providers={"*": _seller()})
        self.assertTrue(_prices_what_we_buy(table))

    def test_a_fallback_seller_is_checked_too(self) -> None:
        os.environ["PROCRAFILER_AI_TRANSCRIBE_FALLBACK"] = "ovh:whisper-large-v3-turbo"
        table = PriceTable(providers={"mistral": _seller(), "ovh": _seller(priceable=False)})
        self.assertFalse(_prices_what_we_buy(table))

    def test_every_configured_seller_must_survive_not_merely_one(self) -> None:
        os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"] = "ovh:whisper-large-v3-turbo"
        table = PriceTable(providers={"mistral": _seller(priceable=False), "ovh": _seller()})
        self.assertFalse(_prices_what_we_buy(table))


class UnconfiguredGuardTests(_EnvIsolated):
    """A fresh install checking prices before it has chosen a provider."""

    def test_a_wholly_mangled_file_is_still_refused(self) -> None:
        table = PriceTable(providers={"mistral": _seller(priceable=False)})
        self.assertFalse(_prices_what_we_buy(table))

    def test_a_healthy_file_is_still_accepted(self) -> None:
        self.assertTrue(_prices_what_we_buy(PriceTable(providers={"mistral": _seller()})))

    def test_a_purely_local_installation_is_treated_the_same(self) -> None:
        """Ollama is never in the table — it is free and runs on the user's own
        machine — so a local-only setup names no seller to be specific about."""
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "ollama:qwen3.5:9b"
        self.assertTrue(_prices_what_we_buy(PriceTable(providers={"mistral": _seller()})))
        self.assertFalse(
            _prices_what_we_buy(PriceTable(providers={"mistral": _seller(priceable=False)}))
        )


class ScopedDatesTests(unittest.TestCase):
    """Freshness belongs to the seller, so a question about it must name one."""

    TABLE = PriceTable(providers={
        "mistral": _seller(updated="2026-08-04", source="https://mistral.ai/pricing/api"),
        "edenai": _seller(updated="2024-01-01", source="https://api.edenai.run/v3/models"),
    })

    def test_the_date_is_our_sellers_not_the_oldest_stranger(self) -> None:
        self.assertEqual(self.TABLE.as_of(["mistral"]), "2026-08-04")

    def test_unscoped_it_is_still_the_oldest(self) -> None:
        """Anti-vacuity: the rule did not change, only who it is applied to. A table
        is still only as trustworthy as its stalest part."""
        self.assertEqual(self.TABLE.as_of(), "2024-01-01")

    def test_two_of_our_sellers_still_give_the_older_of_the_two(self) -> None:
        table = PriceTable(providers={
            "mistral": _seller(updated="2026-08-04"),
            "ovh": _seller(updated="2026-02-01"),
            "edenai": _seller(updated="2020-01-01"),
        })
        self.assertEqual(table.as_of(["mistral", "ovh"]), "2026-02-01")

    def test_an_old_stranger_does_not_age_our_figures(self) -> None:
        self.assertEqual(self.TABLE.age_days(date(2026, 8, 10), providers=["mistral"]), 6)
        self.assertFalse(self.TABLE.is_stale(date(2026, 8, 10), providers=["mistral"]))

    def test_our_own_figures_going_old_is_still_reported(self) -> None:
        """Anti-vacuity: scoping must not amount to switching staleness off."""
        self.assertTrue(self.TABLE.is_stale(date(2027, 8, 10), providers=["mistral"]))
        self.assertTrue(self.TABLE.is_stale(date(2026, 8, 10)), "unscoped, EdenAI is ancient")

    def test_the_source_page_is_the_sellers_own(self) -> None:
        """Sending someone to Mistral's pricing page about an OVH rate is worse than
        sending them nowhere."""
        self.assertEqual(self.TABLE.sources_of(["mistral"]), "https://mistral.ai/pricing/api")
        self.assertNotIn("edenai", self.TABLE.sources_of(["mistral"]))

    def test_a_seller_the_table_does_not_carry_falls_back_to_the_whole_table(self) -> None:
        """Rather than "unknown date", which would read as a defect when the real
        situation is that the question does not apply."""
        self.assertEqual(self.TABLE.as_of(["nobody"]), "2024-01-01")

    def test_a_star_bucket_answers_for_a_named_seller(self) -> None:
        """Beside a real seller on purpose. With `*` alone, dropping it would fall
        through to "use the whole table" and give the same answer by accident —
        the assertion would pass while the lookup was gone."""
        table = PriceTable(providers={
            "*": _seller(updated="2026-08-04"),
            "edenai": _seller(updated="2020-01-01"),
        })
        self.assertEqual(table.as_of(["mistral"]), "2026-08-04")

    def test_an_empty_table_still_says_it_does_not_know(self) -> None:
        self.assertEqual(PriceTable().as_of(["mistral"]), "unknown date")
        self.assertIsNone(PriceTable().age_days(providers=["mistral"]))


class ForecastRecordsItsSellersTests(_EnvIsolated):
    """Through `forecast_cost` itself, not a hand-built forecast.

    Everything below this line reads `priced_providers`; nothing else proves it is
    ever filled in. A forecast that quietly recorded nobody would fall back to
    reading the whole table and lose the entire fix, with every other test still
    green."""

    TABLE = PriceTable(providers={
        "mistral": _seller(updated=_TODAY, model="chat", source="https://mistral.ai/pricing/api"),
        "edenai": _seller(updated="2020-01-01"),
    })

    def test_a_priced_run_names_the_seller_it_was_priced_against(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:chat"
        forecast = forecast_cost(
            AICallEstimate(files=2, analyses=2, billed_tasks=frozenset({"ANALYSIS"})),
            table=self.TABLE,
        )
        assert forecast is not None
        self.assertEqual(forecast.priced_providers, frozenset({"mistral"}))

    def test_a_transcription_names_its_seller_too(self) -> None:
        """The other branch: TRANSCRIBE is priced by duration, several lines away
        from the token path, and was the site of an earlier symbol bug."""
        os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"] = "mistral:chat"
        forecast = forecast_cost(
            AICallEstimate(
                av_files=1, audio_seconds=600, transcribe_calls=1,
                billed_tasks=frozenset({"TRANSCRIBE"}),
            ),
            table=self.TABLE,
        )
        assert forecast is not None
        self.assertEqual(forecast.priced_providers, frozenset({"mistral"}))

    def test_a_local_run_names_nobody(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "ollama:qwen3.5:9b"
        forecast = forecast_cost(
            AICallEstimate(files=2, analyses=2), table=self.TABLE
        )
        assert forecast is not None
        self.assertEqual(forecast.priced_providers, frozenset())

    def test_end_to_end_a_stale_stranger_never_reaches_the_user(self) -> None:
        """The whole point, from an estimate to the sentence printed on screen."""
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:chat"
        forecast = forecast_cost(
            AICallEstimate(files=2, analyses=2, billed_tasks=frozenset({"ANALYSIS"})),
            table=self.TABLE,
        )
        line = format_cost_forecast(forecast)
        self.assertIn(_TODAY, line)
        self.assertNotIn("2020-01-01", line)
        self.assertNotIn("days old", line)


class StaleForecastTests(unittest.TestCase):
    """What the user is shown when figures get old."""

    @staticmethod
    def _forecast(table: PriceTable, seller: str) -> CostForecast:
        return CostForecast(
            table=table, low=1.0, high=1.0, billed_calls_high=4,
            currencies=frozenset({"USD"}), currency_symbol="$",
            priced_providers=frozenset({seller}),
        )

    def test_an_old_table_warns_instead_of_crashing(self) -> None:
        """`table.source` was an AttributeError waiting for the first stale table —
        it would have arrived by itself, in front of a user, six months after the
        last successful price refresh."""
        table = PriceTable(providers={"mistral": _seller(
            updated="2024-01-01", source="https://mistral.ai/pricing/api"
        )})
        line = format_cost_forecast(self._forecast(table, "mistral"))
        self.assertIn("days old", line)
        self.assertIn("https://mistral.ai/pricing/api", line)

    def test_a_table_with_no_source_page_still_says_something(self) -> None:
        table = PriceTable(providers={"mistral": _seller(updated="2024-01-01")})
        self.assertIn("the provider", format_cost_forecast(self._forecast(table, "mistral")))

    def test_a_stale_stranger_does_not_warn_about_our_fresh_rates(self) -> None:
        table = PriceTable(providers={
            "mistral": _seller(updated=_TODAY),
            "edenai": _seller(updated="2020-01-01"),
        })
        line = format_cost_forecast(self._forecast(table, "mistral"))
        self.assertNotIn("days old", line)
        self.assertIn(_TODAY, line, "the date shown must be the one we priced against")

    def test_the_warning_still_fires_when_it_is_our_seller_that_is_old(self) -> None:
        """Anti-vacuity for the same table, priced against the old seller instead."""
        table = PriceTable(providers={
            "mistral": _seller(updated=_TODAY),
            "edenai": _seller(updated="2020-01-01"),
        })
        self.assertIn("days old", format_cost_forecast(self._forecast(table, "edenai")))

    def test_a_forecast_that_names_no_seller_reads_the_whole_table(self) -> None:
        """Older callers built a forecast without recording who priced it; they must
        keep the behaviour they had rather than silently lose the warning."""
        table = PriceTable(providers={"mistral": _seller(updated="2024-01-01")})
        forecast = CostForecast(
            table=table, low=1.0, high=1.0, billed_calls_high=4,
            currencies=frozenset({"USD"}), currency_symbol="$",
        )
        self.assertIn("days old", format_cost_forecast(forecast))


if __name__ == "__main__":
    unittest.main()
