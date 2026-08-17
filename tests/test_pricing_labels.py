# pyright: reportUnknownVariableType=false
"""The published table stopped speaking API model ids.

Its keys were never normalised — they are "whatever the source itself states" —
and Mistral's source is a marketing page, so an entry is now called `mistral small
4` or `ocr 4.1 / ocr`. The `-latest` aliases were removed from the file on purpose:
keeping them meant a human deciding every week which label an alias resolves to,
which is exactly the maintenance the companion repository exists to avoid.

Measured against the live file before any of this was written: all four models
ProcraFiler calls by default priced to None, and the download guard accepted the
file anyway — it asks whether a SELLER can price anything, and Mistral could price
thirty-one things we never buy. So a working table would have been replaced by one
that cannot cost a single run, on the next weekly refresh, everywhere at once.

The mapping is kept HERE rather than there because ProcraFiler is what chooses the
models. Two entries can share every word of their name and differ only in how the
request is made — `voxtral mini transcribe 2` and `voxtral mini transcribe
realtime` are one model at 0.003 and 0.006 per audio minute, streaming or not —
and no price file can know which mode this app uses.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.ai_estimate import AICallEstimate
from procrafiler.cost_forecast import forecast_cost, format_cost_forecast
from procrafiler.pricing import (
    ModelPrice,
    PriceTable,
    ProviderPrices,
    load_price_table,
    model_labels,
)

_PACKAGED = Path(__file__).resolve().parent.parent / "src" / "procrafiler" / "data" / "pricing.json"

# The four ProcraFiler configures out of the box (`.env.example`, `user_setup.py`).
_DEFAULT_MODELS = (
    "mistral-small-latest",
    "mistral-medium-latest",
    "mistral-ocr-latest",
    "voxtral-mini-latest",
)


class ShippedTableTests(unittest.TestCase):
    """Whatever else changes, the models we ship defaults for must have a price."""

    def setUp(self) -> None:
        self.table = load_price_table()
        assert self.table is not None
        self.assertIsNotNone(self.table)

    def test_every_default_model_can_still_be_priced(self) -> None:
        for model in _DEFAULT_MODELS:
            with self.subTest(model=model):
                price = self.table.price_for("mistral", model)
                self.assertIsNotNone(price, f"{model} has no price — a run cannot be costed")
                assert price is not None
                self.assertTrue(price.is_priceable)

    def test_the_shipped_table_is_keyed_by_the_sellers_own_names(self) -> None:
        """Anti-vacuity for the whole file: if the packaged table were still keyed by
        API ids, every test below would pass without the mapping doing anything."""
        keys = set(json.loads(_PACKAGED.read_text("utf-8"))["providers"]["mistral"]["models"])
        for model in _DEFAULT_MODELS:
            with self.subTest(model=model):
                self.assertNotIn(model, keys)

    def test_the_ocr_lookup_takes_the_model_not_the_product(self) -> None:
        """`ocr 4.1 / ocr` is 4.00 per thousand pages and `ocr 4.1 / document ai` is
        5.00 — the same engine sold two ways, both beginning with the same words.
        Choosing the wrong one overcharges every scanned page by 25%."""
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


class LabelResolutionTests(unittest.TestCase):
    def _table(self, **labels: str) -> PriceTable:
        return PriceTable(
            origin="test",
            providers={
                "mistral": ProviderPrices(
                    currency="USD",
                    models={
                        "mistral small 4": ModelPrice(in_per_mtok=0.15, out_per_mtok=0.6),
                        "some product": ModelPrice(per_1k_pages=9.0, kind="product"),
                        "mistral-small-latest": ModelPrice(in_per_mtok=99.0),
                    },
                )
            },
            model_labels=dict(labels),
        )

    def test_a_mapped_model_resolves_to_its_label(self) -> None:
        table = PriceTable(
            origin="test",
            providers={"mistral": ProviderPrices(models={"mistral small 4": ModelPrice(in_per_mtok=0.15)})},
            model_labels={"mistral:mistral-small-latest": "mistral small 4"},
        )
        price = table.price_for("mistral", "mistral-small-latest")
        assert price is not None
        self.assertEqual(price.in_per_mtok, 0.15)

    def test_the_model_id_wins_over_the_label(self) -> None:
        """A `pricing.json` the user wrote is keyed by real model ids. It priced
        correctly before the mapping existed and must keep doing so — the mapping is
        a fallback, not a redirection."""
        table = self._table(**{"mistral:mistral-small-latest": "mistral small 4"})
        price = table.price_for("mistral", "mistral-small-latest")
        assert price is not None
        self.assertEqual(price.in_per_mtok, 99.0, "the label overrode the user's own key")

    def test_a_label_pointing_at_a_product_yields_no_price(self) -> None:
        """A wrong mapping must fail visibly rather than quote a product's rate."""
        table = self._table(**{"mistral:mistral-small-latest": "some product"})
        # The user's own key for that model is removed, so only the label remains.
        del table.providers["mistral"].models["mistral-small-latest"]
        self.assertIsNone(table.price_for("mistral", "mistral-small-latest"))

    def test_an_unmapped_model_has_no_price_rather_than_a_guessed_one(self) -> None:
        table = self._table()
        del table.providers["mistral"].models["mistral-small-latest"]
        self.assertIsNone(table.price_for("mistral", "mistral-small-latest"))

    def test_a_label_is_matched_per_seller(self) -> None:
        """The same model id costs different amounts depending on who serves it, so a
        mapping made for one seller must not price another's."""
        table = self._table(**{"ovh:mistral-small-latest": "mistral small 4"})
        del table.providers["mistral"].models["mistral-small-latest"]
        self.assertIsNone(table.price_for("mistral", "mistral-small-latest"))


class LabelFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)

    def test_the_shipped_mapping_is_loaded(self) -> None:
        self.assertEqual(model_labels()["mistral:mistral-ocr-latest"], "ocr 4.1 / ocr")

    def test_the_users_entries_are_MERGED_not_substituted(self) -> None:
        """Unlike `pricing.json`, which wins whole. Someone mapping the one model we
        do not ship must not thereby lose the four we do."""
        (self.config / "price_labels.json").write_text(
            json.dumps({"mistral:my-own-model": "mistral large 3"}), encoding="utf-8"
        )
        labels = model_labels(self.config)
        self.assertEqual(labels["mistral:my-own-model"], "mistral large 3")
        self.assertEqual(labels["mistral:mistral-ocr-latest"], "ocr 4.1 / ocr", "the shipped map was lost")

    def test_a_user_entry_overrides_a_shipped_one(self) -> None:
        (self.config / "price_labels.json").write_text(
            json.dumps({"mistral:mistral-ocr-latest": "ocr 4.1 / document ai"}), encoding="utf-8"
        )
        self.assertEqual(model_labels(self.config)["mistral:mistral-ocr-latest"], "ocr 4.1 / document ai")

    def test_a_broken_label_file_costs_a_price_not_a_run(self) -> None:
        (self.config / "price_labels.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(model_labels(self.config)["mistral:mistral-ocr-latest"], "ocr 4.1 / ocr")

    def test_the_explanatory_key_in_the_shipped_file_is_not_a_mapping(self) -> None:
        """It documents the file for whoever opens it. A key with no `provider:` in
        it cannot be a mapping, which is what keeps it out of the way."""
        self.assertNotIn("_README", model_labels())

    def test_labels_reach_a_table_loaded_from_a_config_dir(self) -> None:
        table = load_price_table(self.config)
        assert table is not None
        self.assertIsNotNone(table.price_for("mistral", "mistral-ocr-latest"))


class ForecastReportingTests(unittest.TestCase):
    """A hand-kept mapping that nothing displays is a mapping nobody checks."""

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

    def test_the_report_names_the_label_each_model_was_priced_under(self) -> None:
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
