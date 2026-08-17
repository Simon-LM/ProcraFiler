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
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_PACKAGED_TABLE = Path(__file__).resolve().parent / "data" / "pricing.json"
_PACKAGED_FAMILIES = Path(__file__).resolve().parent / "data" / "price_labels.json"

# The version number that follows a family name in a published key: the `4` of
# `mistral small 4`, the `3.5` of `mistral medium 3.5`, the `4.1` of `ocr 4.1 /
# ocr`. It is the only place a generation is stated — the format has no version
# field, because it publishes what the seller's page says and that is a name.
_VERSION_AFTER_FAMILY = re.compile(r"\s+(\d+(?:\.\d+)?)\b")

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

        What makes it load-bearing is `resolve_family`, which finds a rate by
        scanning the keys a seller publishes instead of being told one. `ocr 4.1 /
        ocr` (4.00 per thousand pages) and `ocr 4.1 / document ai` (5.00, `kind:
        product`) are the same OCR engine sold two ways, under the SAME generation
        number — so the number cannot separate them and, without `kind`, a scan of
        the `ocr` family would be choosing between 4.00 and 5.00 on nothing. You
        can send `model: "mistral-ocr-latest"`; you cannot send `model:
        "document ai"`.

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
    # `"provider:model"` -> the FAMILY its price is published under, from
    # `data/price_labels.json`. Not the key itself: see `resolve_family`.
    model_families: dict[str, str] = field(default_factory=dict)

    def provider(self, provider: str) -> ProviderPrices | None:
        return self.providers.get(provider)

    def resolve_family(self, provider: str, family: str) -> str | None:
        """The current key of a family — its newest generation, read from the feed.

        The published table keys each entry by **whatever the source itself states**,
        which for Mistral is the label printed on its pricing page: `mistral small
        4`, `ocr 4.1 / ocr`. Those are not API model ids and never were, and the
        `-latest` aliases were dropped from the file on purpose — keeping them meant
        a human deciding, every week, which label an alias resolves to.

        So the generation is read rather than recorded. `mistral small 4` becomes
        `mistral small 5` and this follows it with no release, which matters because
        these lines do move: `ocr 4` became `ocr 4.1` within days.

        Three rules, each one forced by a case in the live file:

        - **Skip what is not a model.** `ocr 4.1 / ocr` and `ocr 4.1 / document ai`
          share generation 4.1, so the number cannot separate 4.00 from 5.00 —
          `kind` can.
        - **Prefer a generation still published, but fall back on a withdrawn one.**
          `voxtral mini transcribe 2` carries `absent_since` today and its only
          living sibling, `voxtral mini transcribe realtime`, has no number at all
          (it is the same model billed for streaming, at double). Skipping withdrawn
          rows outright would lose that price rather than age it.
        - **Refuse a tie.** Two models at the same top generation is not a decision
          this can make, and picking by name order would be a coin flip on real
          money. No price is the honest answer, and it is reported as one.
        """
        prices = self.providers.get(provider)
        if prices is None:
            return None

        # (generation, key, still published)
        found: list[tuple[float, str, bool]] = []
        for key, price in prices.models.items():
            if not key.startswith(family) or not price.is_model:
                continue
            version = _VERSION_AFTER_FAMILY.match(key[len(family):])
            if version is None:
                continue
            found.append((float(version.group(1)), key, price.absent_since is None))
        if not found:
            return None

        pool = [entry for entry in found if entry[2]] or found
        newest = max(entry[0] for entry in pool)
        at_newest = [key for version, key, _ in pool if version == newest]
        return at_newest[0] if len(at_newest) == 1 else None

    def label_for(self, provider: str, model: str) -> str | None:
        """The table key this model is priced under, when it is not the model id."""
        family = self.model_families.get(f"{provider}:{model}")
        if family is None:
            return None
        return self.resolve_family(provider, family)

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


def model_families(path: Path = _PACKAGED_FAMILIES) -> dict[str, str]:
    """`"provider:model"` -> the family its price is published under.

    Shipped with the app and maintained with it; there is no per-user version. What
    it records is which line of a seller's price list corresponds to a model this
    app calls, which is knowledge about ProcraFiler's own choices, not a setting.
    Nobody installing a document filer should have to learn how a price feed names
    its rows, and the one thing a user could once repair here cannot happen: the
    feed never deletes a key, so a price is never lost — it ages, and says so.

    Unusable content is ignored rather than raised: a malformed file must cost a
    price, never a run. Entries are objects so that each states its own semantics,
    and `feed_latest` is the same field name the other consumer of this feed uses.
    """
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    families: dict[str, str] = {}
    for key, spec in payload.items():
        # A key with no `provider:` in it is not a mapping — that is what keeps the
        # file's own `_README` out of the way of whoever opens it.
        if not isinstance(key, str) or ":" not in key or not isinstance(spec, dict):
            continue
        family = spec.get("feed_latest")
        if isinstance(family, str) and family:
            families[key] = family
    return families


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
    # Attached here rather than parsed with the table: the families live in their own
    # file, and they apply to whichever table won above — including a user's.
    return replace(table, model_families=model_families())


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
