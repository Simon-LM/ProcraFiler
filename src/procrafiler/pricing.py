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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_PACKAGED_TABLE = Path(__file__).resolve().parent / "data" / "pricing.json"
_PACKAGED_LABELS = Path(__file__).resolve().parent / "data" / "price_labels.json"

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
    # The day the source stopped offering this entry. The published format states
    # what that implies in as many words: "every price beside it is the last one
    # observed, not a current one". A figure quoted from such a row is therefore
    # still the best estimate available AND no longer a current rate, and a forecast
    # that shows the first without the second is overstating what it knows.
    absent_since: str | None = None
    # Absent on a model, which is the normal case. `"product"` marks a billable
    # thing that is NOT a model — web search, code execution, image generation,
    # Mistral's Document AI offer.
    kind: str | None = None

    @property
    def is_model(self) -> bool:
        """Whether this row is something you can name in an API request.

        Load-bearing since the published table began keying models by the label
        printed on the seller's pricing page. `ocr 4.1 / ocr` (4.00 per thousand
        pages) and `ocr 4.1 / document ai` (5.00, `kind: product`) are the same OCR
        engine sold two ways, and both begin with the same words — so a lookup that
        ignored `kind` could price every scanned page 25% too high. You can send
        `model: "mistral-ocr-latest"`; you cannot send `model: "document ai"`.

        An explicit `kind: "model"` is accepted for the day the format states the
        normal case rather than leaving it absent.
        """
        return self.kind is None or self.kind == "model"

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
    # `"provider:model"` -> the key that model is priced under in the table. Loaded
    # from `data/price_labels.json`, extendable in the user's config directory. See
    # `label_for` for why this cannot be derived.
    model_labels: dict[str, str] = field(default_factory=dict)

    def provider(self, provider: str) -> ProviderPrices | None:
        return self.providers.get(provider)

    def label_for(self, provider: str, model: str) -> str | None:
        """The table key this model is priced under, when it is not the model id.

        The published table keys each entry by **whatever the source itself states**
        — which, for Mistral, is now the label printed on its pricing page:
        `mistral small 4`, `ocr 4.1 / ocr`, `voxtral mini transcribe 2`. Those are
        not API model ids and never were; `-latest` aliases were dropped from the
        file because keeping them meant a human deciding, every week, which label an
        alias currently points at.

        That decision has to be made somewhere, and this is the right somewhere:
        ProcraFiler is what chooses the models, so ProcraFiler is what knows. The
        published file stays a faithful, automatic transcript of the seller's page.

        Deriving the mapping was tried and rejected. Two entries can share every
        word of their name and differ only in HOW the model is called — `voxtral
        mini transcribe 2` and `voxtral mini transcribe realtime` are one model at
        0.003 and 0.006 per audio minute, the difference being whether the request
        streams. Nothing in a price file can say which mode this app uses; that fact
        lives in `ai_transcribe.py`. A matcher comparing names would have to guess,
        and guessing wrong doubles the quoted price.
        """
        return self.model_labels.get(f"{provider}:{model}")

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        """Exact match on BOTH, then on the mapped label, with one fallback.

        `mistral-medium-latest` is an ALIAS that will one day resolve to a
        different model at a different price, so guessing from a prefix would mean
        confidently pricing something we have never seen. The seller is matched
        just as exactly, because the same model id costs different amounts — and is
        billed in a different currency — depending on who serves it.

        The model id is tried FIRST, at both keys, before the label is considered:
        a `pricing.json` the user wrote themselves is keyed by real model ids, and
        must keep working exactly as it did.

        The fallback is the `*` bucket: a schema-1 file, or a rate the user wrote
        down themselves, names a model without naming a seller, and refusing to use
        it would throw away the one figure they explicitly asked for.

        Rows that are not models are never returned — see `ModelPrice.is_model`.
        """
        label = self.label_for(provider, model)
        for key in (model, label):
            if key is None:
                continue
            for name in (provider, ANY_PROVIDER):
                prices = self.providers.get(name)
                if prices is None:
                    continue
                price = prices.models.get(key)
                if price is not None and price.is_model:
                    return price
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

    def _selected(self, providers: Iterable[str] | None) -> list[ProviderPrices]:
        """The sellers a question is being asked about.

        `None` means the whole table, which is the right answer only when the
        caller genuinely has no idea who it buys from. Everything about freshness
        is per seller, so a caller that knows should say so — see `age_days`.

        A name the table does not carry falls through to the `*` bucket, then is
        dropped. If NOTHING matches, the whole table is used rather than nothing:
        an answer about a table we cannot price from is moot either way, and
        "unknown date" would read as a defect rather than as an irrelevance.
        """
        if providers is None:
            return list(self.providers.values())
        chosen: list[ProviderPrices] = []
        for name in providers:
            for key in (name, ANY_PROVIDER):
                prices = self.providers.get(key)
                if prices is not None and prices not in chosen:
                    chosen.append(prices)
                    break
        return chosen or list(self.providers.values())

    def age_days(
        self, today: date | None = None, *, providers: Iterable[str] | None = None
    ) -> int | None:
        """How old the OLDEST of the given providers' figures are.

        The oldest rather than the newest: a table is only as trustworthy as its
        stalest part, and a fresh Mistral date must not vouch for an OVH one nobody
        has checked in a year.

        `providers` scopes that to the sellers the run actually buys from, and
        leaving it out is a real mistake once the table carries sellers this app
        cannot even call. The published table now lists four; ProcraFiler talks to
        one. Unscoped, an unmaintained EdenAI block would date — and eventually
        declare stale — a forecast priced entirely against fresh Mistral figures.
        """
        ages: list[int] = []
        for prices in self._selected(providers):
            if not prices.updated:
                continue
            try:
                checked = datetime.strptime(prices.updated, "%Y-%m-%d").date()
            except ValueError:
                continue
            ages.append(((today or datetime.now(timezone.utc).date()) - checked).days)
        return max(ages) if ages else None

    def is_stale(
        self, today: date | None = None, *, providers: Iterable[str] | None = None
    ) -> bool:
        age = self.age_days(today, providers=providers)
        return age is not None and age > STALE_AFTER_DAYS

    def as_of(self, providers: Iterable[str] | None = None) -> str:
        """The oldest date among those providers, for the same reason as `age_days`."""
        dates = [p.updated for p in self._selected(providers) if p.updated]
        return min(dates) if dates else "unknown date"

    def sources_of(self, providers: Iterable[str] | None = None) -> str:
        """The pages these figures were read off, for a message that tells the user
        where to go and check them.

        Per seller, like everything else here: sending someone to Mistral's pricing
        page about a rate that came from OVH's catalog is worse than sending them
        nowhere."""
        seen: list[str] = []
        for prices in self._selected(providers):
            if prices.source and prices.source not in seen:
                seen.append(prices.source)
        return " and ".join(seen)


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
            absent_since=(str(spec["absent_since"]) if spec.get("absent_since") else None),
            kind=(str(spec["kind"]) if spec.get("kind") else None),
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


def _load_labels(path: Path) -> dict[str, str]:
    """`"provider:model"` -> table key, from a JSON object. Unusable content is
    silently ignored: a broken label file must cost a price, never a run."""
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str) and ":" in key and value
    }


def model_labels(config_dir: Path | None = None) -> dict[str, str]:
    """The shipped mapping, with the user's own entries laid over it.

    MERGED rather than replaced, unlike `pricing.json`. The two files answer
    different questions: a hand-written `pricing.json` means "charge me this rate
    instead", so it must win whole, while a label file means "this model is listed
    under that name" — and someone adding the one model we do not ship must not
    thereby lose the mapping for the four we do.
    """
    labels = _load_labels(_PACKAGED_LABELS)
    if config_dir is not None:
        labels.update(_load_labels(config_dir / "price_labels.json"))
    return labels


def load_price_table(config_dir: Path | None = None) -> PriceTable | None:
    """The table in force. None only if even the packaged copy is unusable, in
    which case the app must report costs as unavailable rather than as zero.

    Reads only; the download lives in `pricing_refresh`. Keeping the network out of
    this function means every consumer of a price — a forecast, a usage report, the
    spend ceiling — can call it freely without any of them wondering whether it will
    block.
    """
    table = None
    if config_dir is not None:
        table = _load_file(config_dir / "pricing.json", "your own pricing.json")
        if table is None:
            # The user's own file outranks it: someone who wrote down a negotiated
            # rate must not have it overwritten by a public one, however fresh.
            table = _load_file(config_dir / "pricing.cached.json", "downloaded")
    if table is None:
        table = _load_file(_PACKAGED_TABLE, "shipped with the app")
    if table is None:
        return None
    # Attached here rather than parsed with the table: the labels live in their own
    # file, and they apply to whichever table won above — including a user's.
    return replace(table, model_labels=model_labels(config_dir))


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
