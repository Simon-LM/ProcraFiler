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
2. **a refreshed copy** — `<config>/pricing.cached.json`, downloaded weekly from
   the companion repository described in `docs/ai-pricing-source.md` (see
   `pricing_refresh`). Present only once a refresh has succeeded;
3. **the table shipped in the package** — always present, works offline, dated.

Prices are held **per provider**, because the same model is sold by more than
one. `mistral-small-latest` served by Mistral and a Mistral model served by OVH
are different prices, and — the part a flat table cannot express at all —
different CURRENCIES: Mistral publishes in USD, OVH in EUR. Currency, source page
and freshness date all belong to the seller, not to the model.

An unknown model yields **no price at all**, never zero. A run whose model is
missing from the table must say it cannot price itself; quietly reporting $0.00
would be the one failure that actively misleads. The single legitimate zero is a
model the provider declares FREE, which is a fact rather than an absence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    # Transcription bills by recording length. A model may declare this AND token
    # prices — Voxtral Small does — and is then charged for both.
    per_audio_minute: float | None = None
    # The same unit, per second: OVH prices Whisper that way. Kept as published
    # rather than converted, so a figure can be checked against its source page.
    per_audio_second: float | None = None
    # Declared free by the provider. NOT the same as absent: "this costs nothing"
    # is a fact, "I have no price for this" is an admission, and the whole point of
    # this module is that the two never look alike.
    free: bool = False

    @property
    def is_priceable(self) -> bool:
        if self.free:
            return True
        return any(
            value is not None
            for value in (
                self.in_per_mtok, self.out_per_mtok, self.per_1k_pages,
                self.per_audio_minute, self.per_audio_second,
            )
        )


# A price that applies whatever the seller. This is what a schema-1 file means:
# it listed model ids with no notion of who serves them, so its figures are taken
# as valid for any provider. It is also what a user's own hand-written file means
# when they simply wrote down a rate they were quoted.
ANY_PROVIDER = "*"


@dataclass(frozen=True)
class ProviderPrices:
    """What one seller charges, in its own currency, read from its own page."""

    currency: str = "USD"
    currency_symbol: str = "$"
    updated: str = ""
    checked_utc: str = ""
    source: str = ""
    models: dict[str, ModelPrice] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceTable:
    origin: str = "packaged"
    providers: dict[str, ProviderPrices] = field(default_factory=dict)

    def provider(self, provider: str) -> ProviderPrices | None:
        return self.providers.get(provider)

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        """Exact match on BOTH, with one deliberate fallback.

        `mistral-medium-latest` is an ALIAS that will one day resolve to a
        different model at a different price, so guessing from a prefix would mean
        confidently pricing something we have never seen. The seller is matched
        just as exactly, because the same model id costs different amounts — and is
        billed in a different currency — depending on who serves it.

        The fallback is the `*` bucket: a schema-1 file, or a rate the user wrote
        down themselves, names a model without naming a seller, and refusing to use
        it would throw away the one figure they explicitly asked for.
        """
        for name in (provider, ANY_PROVIDER):
            prices = self.providers.get(name)
            if prices is not None and model in prices.models:
                return prices.models[model]
        return None

    def currency_of(self, provider: str) -> str:
        for name in (provider, ANY_PROVIDER):
            prices = self.providers.get(name)
            if prices is not None:
                return prices.currency
        return "USD"

    def symbol_of(self, provider: str) -> str:
        for name in (provider, ANY_PROVIDER):
            prices = self.providers.get(name)
            if prices is not None:
                return prices.currency_symbol
        return "$"

    def cost(
        self,
        provider: str,
        model: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        pages: int = 0,
        audio_seconds: int = 0,
    ) -> float | None:
        """Cost of one model's consumption, or None when it cannot be priced.

        The figure is in `currency_of(provider)`. Nothing here converts anything:
        summing a USD line and a EUR line is the caller's problem, and the caller
        is expected to refuse rather than guess an exchange rate.
        """
        price = self.price_for(provider, model)
        if price is None or not price.is_priceable:
            return None
        if price.free:
            # The one legitimate zero in this module.
            return 0.0
        total = 0.0
        if price.in_per_mtok is not None:
            total += tokens_in / 1_000_000 * price.in_per_mtok
        if price.out_per_mtok is not None:
            total += tokens_out / 1_000_000 * price.out_per_mtok
        if price.per_1k_pages is not None:
            total += pages / 1_000 * price.per_1k_pages
        if price.per_audio_minute is not None:
            total += audio_seconds / 60 * price.per_audio_minute
        if price.per_audio_second is not None:
            total += audio_seconds * price.per_audio_second
        # Each unit is charged if and only if the TABLE declares a price for it.
        # That is what keeps a transcription honest: Voxtral Mini's reply reports
        # token counts, but its entry carries no token price, so they cost nothing.
        # A model that genuinely bills both ways — Voxtral Small, per audio minute
        # AND per text token — declares both and is charged for both. Special-casing
        # "duration means duration only" here looked safer and was simply wrong.
        return total

    @property
    def models(self) -> dict[str, ModelPrice]:
        """Every model of every provider, flattened. For inspection only — a lookup
        must go through `price_for`, or two sellers of the same id collapse into
        one arbitrary price."""
        merged: dict[str, ModelPrice] = {}
        for prices in self.providers.values():
            merged.update(prices.models)
        return merged

    def age_days(self, today: date | None = None) -> int | None:
        """How old the OLDEST provider's figures are.

        The oldest rather than the newest: a table is only as trustworthy as its
        stalest part, and a fresh Mistral date must not vouch for an OVH one nobody
        has checked in a year.
        """
        ages: list[int] = []
        for prices in self.providers.values():
            if not prices.updated:
                continue
            try:
                checked = datetime.strptime(prices.updated, "%Y-%m-%d").date()
            except ValueError:
                continue
            ages.append(((today or datetime.now(timezone.utc).date()) - checked).days)
        return max(ages) if ages else None

    def is_stale(self, today: date | None = None) -> bool:
        age = self.age_days(today)
        return age is not None and age > STALE_AFTER_DAYS

    def as_of(self) -> str:
        """The oldest provider date, for the same reason as `age_days`."""
        dates = [p.updated for p in self.providers.values() if p.updated]
        return min(dates) if dates else "unknown date"


# Schemas this app knows how to read. A file announcing anything else is IGNORED
# rather than read optimistically: a field that changed meaning would be applied
# silently to real money.
SUPPORTED_SCHEMAS = (1, 2)


def _parse_models(raw_models: Any) -> dict[str, ModelPrice]:
    if not isinstance(raw_models, dict):
        return {}
    models: dict[str, ModelPrice] = {}
    for model_id, spec in raw_models.items():
        if not isinstance(spec, dict):
            continue
        models[str(model_id)] = ModelPrice(
            display_name=str(spec.get("display_name") or model_id),
            in_per_mtok=_as_price(spec.get("in_per_mtok")),
            out_per_mtok=_as_price(spec.get("out_per_mtok")),
            per_1k_pages=_as_price(spec.get("per_1k_pages")),
            per_audio_minute=_as_price(spec.get("per_audio_minute")),
            per_audio_second=_as_price(spec.get("per_audio_second")),
            free=spec.get("free") is True,
        )
    return models


# Currencies the app can print a symbol for. Anything else is shown by its code,
# which is correct and readable — inventing a symbol for a currency we do not know
# would be worse than "12.30 CHF".
_SYMBOLS = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}


def _parse_provider(spec: Any) -> ProviderPrices | None:
    if not isinstance(spec, dict):
        return None
    models = _parse_models(spec.get("models"))
    if not models:
        return None
    currency = str(spec.get("currency") or "USD")
    return ProviderPrices(
        currency=currency,
        currency_symbol=str(spec.get("currency_symbol") or _SYMBOLS.get(currency, currency + " ")),
        updated=str(spec.get("updated") or ""),
        checked_utc=str(spec.get("checked_utc") or ""),
        source=str(spec.get("source") or ""),
        models=models,
    )


def _parse_table(payload: Any, origin: str) -> PriceTable | None:
    """Read a price file of either schema.

    **Schema 2** keys everything by provider, because currency, source page and
    freshness belong to the seller: Mistral publishes in USD and OVH in EUR, and no
    single top-level `currency` can be true for both.

    **Schema 1** had no notion of a seller. Its models are kept under the `*`
    bucket — "these figures apply whoever serves them" — which is exactly what such
    a file meant, and what a user's own hand-written rate still means. Without it,
    every existing `pricing.json` in a config directory would stop being read the
    day the app learned about providers.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") not in (None, *SUPPORTED_SCHEMAS):
        return None

    raw_providers = payload.get("providers")
    if isinstance(raw_providers, dict):
        providers: dict[str, ProviderPrices] = {}
        for name, spec in raw_providers.items():
            parsed = _parse_provider(spec)
            if parsed is not None:
                providers[str(name)] = parsed
        return PriceTable(origin=origin, providers=providers) if providers else None

    legacy = _parse_provider(payload)
    return PriceTable(origin=origin, providers={ANY_PROVIDER: legacy}) if legacy else None


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
    which case the app must report costs as unavailable rather than as zero.

    Reads only; the download lives in `pricing_refresh`. Keeping the network out of
    this function means every consumer of a price — a forecast, a usage report, the
    spend ceiling — can call it freely without any of them wondering whether it will
    block.
    """
    if config_dir is not None:
        user_table = _load_file(config_dir / "pricing.json", "your own pricing.json")
        if user_table is not None:
            return user_table
        # The user's own file outranks it: someone who wrote down a negotiated rate
        # must not have it overwritten by a public one, however fresh.
        cached = _load_file(config_dir / "pricing.cached.json", "downloaded")
        if cached is not None:
            return cached
    return _load_file(_PACKAGED_TABLE, "shipped with the app")


def format_amount(amount: float, symbol: str = "$") -> str:
    """Small amounts are the normal case — a handful of files is cents — and
    rounding them to two decimals would show `$0.00` for a real charge.

    Takes the SYMBOL rather than the table, because a table no longer has one
    currency: what a figure is denominated in depends on which provider produced
    it.
    """
    if amount and abs(amount) < 0.01:
        return f"<{symbol}0.01"
    return f"{symbol}{amount:.2f}"
