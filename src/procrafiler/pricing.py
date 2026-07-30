"""What the providers charge — a table, with the date it was true.

The app cannot know today's prices. There is no machine-readable pricing endpoint
at Mistral, and the figures live on a public marketing page a human has to read.
So this module does the only honest thing available: it holds a table, it records
**when that table was last verified**, and it makes every consumer carry that date
into whatever it displays. `$0.42` is a claim the app cannot support; `$0.42
(rates of 2026-07-30)` is one it can.

Three sources, first match wins:

1. **the user's own file** — `<config>/pricing.json`. Always wins, for negotiated
   rates, another provider, or simply because they read the page more recently
   than we did;
2. **a refreshed copy** — reserved for the companion repository described in
   `docs/ai-pricing-source.md`; absent today;
3. **the table shipped in the package** — always present, works offline, dated.

An unknown model yields **no price at all**, never zero. A run whose model is
missing from the table must say it cannot price itself; quietly reporting $0.00
would be the one failure that actively misleads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_PACKAGED_TABLE = Path(__file__).resolve().parent / "data" / "pricing.json"

# Past this, the displayed date stops being a footnote and becomes a warning. Not
# a hard failure: stale figures the user can see the age of are still far more
# useful than none, and prices move a few times a year.
STALE_AFTER_DAYS = 180


@dataclass(frozen=True)
class ModelPrice:
    display_name: str = ""
    in_per_mtok: float | None = None
    out_per_mtok: float | None = None
    per_1k_pages: float | None = None

    @property
    def is_priceable(self) -> bool:
        return any(
            value is not None
            for value in (self.in_per_mtok, self.out_per_mtok, self.per_1k_pages)
        )


@dataclass(frozen=True)
class PriceTable:
    currency: str = "USD"
    currency_symbol: str = "$"
    updated: str = ""
    checked_utc: str = ""
    source: str = ""
    origin: str = "packaged"
    models: dict[str, ModelPrice] | None = None

    def price_for(self, model: str) -> ModelPrice | None:
        """Exact key match only. `mistral-medium-latest` is an ALIAS that will one
        day resolve to a different model at a different price, so guessing from a
        prefix would mean confidently pricing something we have never seen."""
        return (self.models or {}).get(model)

    def cost(
        self, model: str, *, tokens_in: int = 0, tokens_out: int = 0, pages: int = 0
    ) -> float | None:
        """Cost of one model's consumption, or None when it cannot be priced."""
        price = self.price_for(model)
        if price is None or not price.is_priceable:
            return None
        total = 0.0
        if price.in_per_mtok is not None:
            total += tokens_in / 1_000_000 * price.in_per_mtok
        if price.out_per_mtok is not None:
            total += tokens_out / 1_000_000 * price.out_per_mtok
        if price.per_1k_pages is not None:
            total += pages / 1_000 * price.per_1k_pages
        return total

    def age_days(self, today: date | None = None) -> int | None:
        if not self.updated:
            return None
        try:
            checked = datetime.strptime(self.updated, "%Y-%m-%d").date()
        except ValueError:
            return None
        return ((today or datetime.now(timezone.utc).date()) - checked).days

    def is_stale(self, today: date | None = None) -> bool:
        age = self.age_days(today)
        return age is not None and age > STALE_AFTER_DAYS

    def as_of(self) -> str:
        return self.updated or "unknown date"


def _parse_table(payload: Any, origin: str) -> PriceTable | None:
    if not isinstance(payload, dict):
        return None
    # A file from a newer schema is IGNORED rather than read optimistically: a
    # field that changed meaning would be applied silently to real money.
    if payload.get("schema_version") not in (None, 1):
        return None
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        return None

    models: dict[str, ModelPrice] = {}
    for model_id, spec in raw_models.items():
        if not isinstance(spec, dict):
            continue
        models[str(model_id)] = ModelPrice(
            display_name=str(spec.get("display_name") or model_id),
            in_per_mtok=_as_price(spec.get("in_per_mtok")),
            out_per_mtok=_as_price(spec.get("out_per_mtok")),
            per_1k_pages=_as_price(spec.get("per_1k_pages")),
        )
    if not models:
        return None
    return PriceTable(
        currency=str(payload.get("currency") or "USD"),
        currency_symbol=str(payload.get("currency_symbol") or "$"),
        updated=str(payload.get("updated") or ""),
        checked_utc=str(payload.get("checked_utc") or ""),
        source=str(payload.get("source") or ""),
        origin=origin,
        models=models,
    )


def _as_price(value: Any) -> float | None:
    """A price must be a positive, finite number. Anything else — a string, a
    negative, an absurd figure — means the file is wrong, and a wrong price is
    worse than a missing one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if number < 0 or number > 10_000:
        return None
    return number


def _load_file(path: Path, origin: str) -> PriceTable | None:
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return _parse_table(payload, origin)


def load_price_table(config_dir: Path | None = None) -> PriceTable | None:
    """The table in force. None only if even the packaged copy is unusable, in
    which case the app must report costs as unavailable rather than as zero."""
    if config_dir is not None:
        user_table = _load_file(config_dir / "pricing.json", "your own pricing.json")
        if user_table is not None:
            return user_table
    return _load_file(_PACKAGED_TABLE, "shipped with the app")


def format_amount(amount: float, table: PriceTable) -> str:
    """Small amounts are the normal case — a handful of files is cents — and
    rounding them to two decimals would show `$0.00` for a real charge."""
    if amount and abs(amount) < 0.01:
        return f"<{table.currency_symbol}0.01"
    return f"{table.currency_symbol}{amount:.2f}"
