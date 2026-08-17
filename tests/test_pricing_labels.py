# pyright: reportUnknownVariableType=false
"""The published table stopped speaking API model ids.

Its keys were never normalised — they are "whatever the source itself states" —
and Mistral's source is a marketing page, so an entry is now called `mistral small
4` or `ocr 4.1 / ocr`. The `-latest` aliases were removed from the file on purpose:
keeping them meant a human deciding every week which label an alias resolves to,
which is exactly the maintenance the companion repository exists to avoid.

Measured against the live file before any of this was written: all four models
ProcraFiler calls priced to None, and the download guard accepted the file anyway
— it asks whether a SELLER can price anything, and Mistral could price thirty
entries we never buy. So a working table would have been replaced by one that
cannot cost a single run, on the next weekly refresh, everywhere at once.

What is recorded here is the FAMILY, never the line: `mistral small`, not `mistral
small 4`. The generation is read from the feed on every run, because these names
move — `ocr 4` became `ocr 4.1` within days — and a recorded generation would mean
a release every time one did. Which family a model belongs to is knowledge about
ProcraFiler's own choices, so it lives with ProcraFiler; the published file stays a
faithful, automatic transcript of a seller's page.

And it is kept to what THIS app calls. The feed serves several projects, so
Mistral's thirty-one entries are not a list of things to map; mapping one
ProcraFiler never sends would ship a resolution nothing exercises, in a module
whose whole point is that a guess and a known rate never look alike.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from procrafiler.ai_estimate import AICallEstimate
from procrafiler.ai_naming import LOCAL_PROVIDERS
from procrafiler.cost_forecast import forecast_cost, format_cost_forecast
from procrafiler.pricing import (
    ModelPrice,
    PriceTable,
    ProviderPrices,
    load_price_table,
    model_families,
)
from procrafiler.user_setup import _MISTRAL_PRESET  # pyright: ignore[reportPrivateUsage]

_REPO = Path(__file__).resolve().parent.parent
_PACKAGED = _REPO / "src" / "procrafiler" / "data" / "pricing.json"
_ENV_EXAMPLE = _REPO / ".env.example"

# The four ProcraFiler configures out of the box (`.env.example`, `user_setup.py`).
_DEFAULT_MODELS = (
    "mistral-small-latest",
    "mistral-medium-latest",
    "mistral-ocr-latest",
    "voxtral-mini-latest",
)


def _billable_models_procrafiler_configures() -> set[str]:
    """`provider:model` for every remote model this app ships a setting for.

    Read from the two files that decide it — the env template and the preset the
    guided setup writes — rather than restated here, so the scope test below moves
    with the app instead of having to be remembered.
    """
    settings = dict(_MISTRAL_PRESET)
    for line in _ENV_EXAMPLE.read_text("utf-8").splitlines():
        name, _, value = line.strip().partition("=")
        if name.startswith("PROCRAFILER_AI_") and name.endswith(("_PRIMARY", "_FALLBACK")):
            settings[name] = value.strip()

    configured: set[str] = set()
    for value in settings.values():
        provider, _, model = value.partition(":")
        if model and provider not in LOCAL_PROVIDERS:
            configured.add(value)
    return configured


class ShippedTableTests(unittest.TestCase):
    """Whatever else changes, the models we ship defaults for must have a price."""

    def setUp(self) -> None:
        table = load_price_table()
        self.assertIsNotNone(table)
        assert table is not None
        self.table = table

    def test_every_default_model_can_still_be_priced(self) -> None:
        for model in _DEFAULT_MODELS:
            with self.subTest(model=model):
                price = self.table.price_for("mistral", model)
                self.assertIsNotNone(price, f"{model} has no price — a run cannot be costed")
                assert price is not None
                self.assertTrue(price.is_priceable)

    def test_the_shipped_table_is_keyed_by_the_sellers_own_names(self) -> None:
        """Anti-vacuity for the whole file: if the packaged table were still keyed by
        API ids, every test below would pass without the families doing anything."""
        keys = set(json.loads(_PACKAGED.read_text("utf-8"))["providers"]["mistral"]["models"])
        for model in _DEFAULT_MODELS:
            with self.subTest(model=model):
                self.assertNotIn(model, keys)

    def test_each_default_model_resolves_to_the_line_it_is_billed_under(self) -> None:
        expected = {
            "mistral-small-latest": "mistral small 4",
            "mistral-medium-latest": "mistral medium 3.5",
            "mistral-ocr-latest": "ocr 4.1 / ocr",
            "voxtral-mini-latest": "voxtral mini transcribe 2",
        }
        for model, label in expected.items():
            with self.subTest(model=model):
                self.assertEqual(self.table.label_for("mistral", model), label)

    def test_the_ocr_lookup_takes_the_model_not_the_product(self) -> None:
        """`ocr 4.1 / ocr` is 4.00 per thousand pages and `ocr 4.1 / document ai` is
        5.00 — the same engine sold two ways, at the SAME generation number, so the
        number cannot separate them. Choosing wrong overcharges every scanned page
        by 25%."""
        price = self.table.price_for("mistral", "mistral-ocr-latest")
        assert price is not None
        self.assertEqual(price.per_1k_pages, 4.0)

    def test_a_product_row_is_never_returned_as_a_model(self) -> None:
        models = self.table.providers["mistral"].models
        self.assertEqual(models["ocr 4.1 / document ai"].kind, "product")
        self.assertFalse(models["ocr 4.1 / document ai"].is_model)
        self.assertTrue(models["ocr 4.1 / ocr"].is_model)

    def test_a_withdrawn_entry_is_read_as_withdrawn(self) -> None:
        price = self.table.price_for("mistral", "voxtral-mini-latest")
        assert price is not None
        self.assertEqual(price.absent_since, "2026-08-17")

    def test_the_transcribe_family_prefers_the_mode_this_app_calls(self) -> None:
        """`voxtral mini transcribe realtime` is the same model billed for streaming,
        at 0.006 against 0.003 — double. It survives in the feed while the numbered
        line is withdrawn, so resolving by "still published" alone would quote twice
        the rate for a call ProcraFiler does not make."""
        price = self.table.price_for("mistral", "voxtral-mini-latest")
        assert price is not None
        self.assertEqual(price.per_audio_minute, 0.003)


class ShippedMappingScopeTests(unittest.TestCase):
    """The shipped mapping covers what ProcraFiler calls — no more, no less.

    The feed is not written for this app alone, so its contents are not a to-do
    list. A family for a model nothing here sends could only be chosen by reading a
    page and picking the closest-looking name, and nothing would ever exercise the
    result: no run quotes it, `doctor` never checks it, so a wrong one would sit
    there indefinitely looking authoritative.
    """

    def test_it_maps_every_model_this_app_configures(self) -> None:
        for key in _billable_models_procrafiler_configures():
            with self.subTest(model=key):
                self.assertIn(key, model_families(), "a model we ship a setting for has no price")

    def test_it_maps_nothing_this_app_does_not_configure(self) -> None:
        configured = _billable_models_procrafiler_configures()
        extra = sorted(set(model_families()) - configured)
        self.assertEqual(extra, [], "mapped a model ProcraFiler never calls — see this class's docstring")

    def test_the_source_of_that_scope_is_not_empty(self) -> None:
        """Anti-vacuity: an unreadable `.env.example` would make both tests above
        pass by describing nothing at all."""
        self.assertEqual(len(_billable_models_procrafiler_configures()), 4)


class FamilyResolutionTests(unittest.TestCase):
    """Which generation of a family is in force, read from the feed every run."""

    def _table(self, models: dict[str, ModelPrice], **families: str) -> PriceTable:
        return PriceTable(
            origin="test",
            providers={"mistral": ProviderPrices(currency="USD", models=models)},
            model_families={f"mistral:{k}": v for k, v in families.items()},
        )

    def test_the_newest_generation_wins(self) -> None:
        table = self._table(
            {
                "widget 3": ModelPrice(in_per_mtok=3.0),
                "widget 4": ModelPrice(in_per_mtok=4.0),
                "widget 3.5": ModelPrice(in_per_mtok=3.5),
            },
            **{"widget-latest": "widget"},
        )
        self.assertEqual(table.label_for("mistral", "widget-latest"), "widget 4")

    def test_a_decimal_generation_outranks_the_integer_it_refines(self) -> None:
        """`ocr 4` became `ocr 4.1`; string ordering would have kept the older one."""
        table = self._table(
            {"widget 4": ModelPrice(in_per_mtok=4.0), "widget 4.1": ModelPrice(in_per_mtok=1.0)},
            **{"widget-latest": "widget"},
        )
        self.assertEqual(table.label_for("mistral", "widget-latest"), "widget 4.1")

    def test_a_still_published_generation_beats_a_newer_withdrawn_one(self) -> None:
        table = self._table(
            {
                "widget 4": ModelPrice(in_per_mtok=4.0),
                "widget 5": ModelPrice(in_per_mtok=5.0, absent_since="2026-08-17"),
            },
            **{"widget-latest": "widget"},
        )
        self.assertEqual(table.label_for("mistral", "widget-latest"), "widget 4")

    def test_a_withdrawn_generation_is_used_when_none_is_published(self) -> None:
        """Ages the price rather than losing it — the feed never deletes a key, and a
        rate that says how old it is beats no rate at all. This is `voxtral mini
        transcribe 2` today."""
        table = self._table(
            {
                "widget 1": ModelPrice(in_per_mtok=1.0, absent_since="2026-01-01"),
                "widget 2": ModelPrice(in_per_mtok=2.0, absent_since="2026-08-17"),
            },
            **{"widget-latest": "widget"},
        )
        self.assertEqual(table.label_for("mistral", "widget-latest"), "widget 2")

    def test_a_row_that_is_not_a_model_is_not_a_candidate(self) -> None:
        table = self._table(
            {
                "widget 4": ModelPrice(in_per_mtok=4.0),
                "widget 5": ModelPrice(in_per_mtok=9.0, kind="product"),
            },
            **{"widget-latest": "widget"},
        )
        self.assertEqual(table.label_for("mistral", "widget-latest"), "widget 4")

    def test_a_sibling_with_no_generation_is_not_a_candidate(self) -> None:
        """`voxtral mini transcribe realtime` is shaped exactly like this."""
        table = self._table(
            {
                "widget 2": ModelPrice(in_per_mtok=2.0),
                "widget realtime": ModelPrice(in_per_mtok=99.0),
            },
            **{"widget-latest": "widget"},
        )
        self.assertEqual(table.label_for("mistral", "widget-latest"), "widget 2")

    def test_a_tie_on_the_newest_generation_is_refused(self) -> None:
        """Two models at the same top generation is not a decision this can make, and
        name order would be a coin flip on real money."""
        table = self._table(
            {"widget 4 / a": ModelPrice(in_per_mtok=4.0), "widget 4 / b": ModelPrice(in_per_mtok=8.0)},
            **{"widget-latest": "widget"},
        )
        self.assertIsNone(table.label_for("mistral", "widget-latest"))
        self.assertIsNone(table.price_for("mistral", "widget-latest"))

    def test_an_empty_family_yields_no_price_rather_than_a_guessed_one(self) -> None:
        table = self._table({"other 1": ModelPrice(in_per_mtok=1.0)}, **{"widget-latest": "widget"})
        self.assertIsNone(table.label_for("mistral", "widget-latest"))

    def test_an_unmapped_model_has_no_price(self) -> None:
        table = self._table({"widget 4": ModelPrice(in_per_mtok=4.0)})
        self.assertIsNone(table.price_for("mistral", "widget-latest"))

    def test_the_model_id_wins_over_the_family(self) -> None:
        """A `pricing.json` the user wrote is keyed by real model ids. It priced
        correctly before any of this existed and must keep doing so — the family is
        a fallback, not a redirection."""
        table = self._table(
            {"widget 4": ModelPrice(in_per_mtok=4.0), "widget-latest": ModelPrice(in_per_mtok=99.0)},
            **{"widget-latest": "widget"},
        )
        price = table.price_for("mistral", "widget-latest")
        assert price is not None
        self.assertEqual(price.in_per_mtok, 99.0, "the family overrode the user's own key")

    def test_a_family_is_matched_per_seller(self) -> None:
        """The same model id costs different amounts depending on who serves it, so a
        family recorded for one seller must not price another's."""
        table = PriceTable(
            origin="test",
            providers={"mistral": ProviderPrices(models={"widget 4": ModelPrice(in_per_mtok=4.0)})},
            model_families={"ovh:widget-latest": "widget"},
        )
        self.assertIsNone(table.price_for("mistral", "widget-latest"))


class FamilyFileTests(unittest.TestCase):
    def test_the_shipped_file_records_families_not_lines(self) -> None:
        """Anti-vacuity for `feed_latest`: a file holding `mistral small 4` would
        make every resolution test above pass while following nothing."""
        families = model_families()
        self.assertEqual(families["mistral:mistral-ocr-latest"], "ocr")
        for key, family in families.items():
            with self.subTest(key=key):
                self.assertIsNone(
                    re.search(r"\d", family),
                    f"{key} records a generation ({family!r}) instead of a family",
                )

    def test_the_explanatory_key_is_not_a_mapping(self) -> None:
        """It documents the file for whoever opens it. A key with no `provider:` in
        it cannot be a mapping, which is what keeps it out of the way."""
        self.assertNotIn("_README", model_families())

    def test_a_broken_file_costs_a_price_not_a_run(self) -> None:
        broken = Path(__file__).resolve().parent / "no-such-price-labels.json"
        self.assertEqual(model_families(broken), {})

    def test_an_entry_in_an_unknown_shape_is_ignored(self) -> None:
        """Tolerated at runtime rather than raised, for the same reason: a malformed
        file must cost a price, never a run. The shipped file is what tests above
        keep correct."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "price_labels.json"
            path.write_text(
                json.dumps(
                    {
                        "mistral:a": "mistral small",
                        "mistral:b": {"nonsense": "x"},
                        "mistral:c": {"feed_latest": ""},
                        "mistral:d": {"feed_latest": "ocr"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(model_families(path), {"mistral:d": "ocr"})


class ForecastReportingTests(unittest.TestCase):
    """A hand-kept family list that nothing displays is a list nobody checks."""

    def setUp(self) -> None:
        self._snapshot = {k: v for k, v in os.environ.items() if k.startswith("PROCRAFILER_")}
        for key in list(os.environ):
            if key.startswith("PROCRAFILER_"):
                del os.environ[key]
        self.addCleanup(lambda: os.environ.update(self._snapshot))
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"] = "mistral:voxtral-mini-latest"

    def _forecast(self):  # noqa: ANN202
        estimate = AICallEstimate(files=4, local_reads=4, analyses=4, av_files=1,
                                  audio_seconds=120, transcribe_calls=1)
        forecast = forecast_cost(estimate)
        assert forecast is not None
        return forecast

    def test_the_report_names_the_line_each_model_was_priced_under(self) -> None:
        line = format_cost_forecast(self._forecast())
        self.assertIn('mistral-small-latest as "mistral small 4"', line)

    def test_the_report_says_a_rate_is_no_longer_published(self) -> None:
        forecast = self._forecast()
        self.assertEqual(forecast.withdrawn_rates.get("voxtral-mini-latest"), "2026-08-17")

        line = format_cost_forecast(forecast)
        self.assertIn("no longer on the seller's price list", line)
        self.assertIn("voxtral-mini-latest, last published 2026-08-17", line)
        self.assertIn("last one seen", line)

    def test_a_current_rate_is_not_announced_as_withdrawn(self) -> None:
        """Anti-vacuity: the note must describe the entry, not appear on every run."""
        del os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"]
        estimate = AICallEstimate(files=4, local_reads=4, analyses=4)
        forecast = forecast_cost(estimate)
        assert forecast is not None
        self.assertEqual(forecast.withdrawn_rates, {})
        self.assertNotIn("no longer on the seller's price list", format_cost_forecast(forecast))


if __name__ == "__main__":
    unittest.main()
