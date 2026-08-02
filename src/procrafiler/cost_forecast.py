"""What a run will cost — in money, before spending any of it.

`ai_estimate` says how many calls a run makes; `pricing` says what a token is
worth. Neither is enough on its own, because prices are per million **tokens** and
a call count is not a token count. This module supplies the missing middle: how
many tokens a call of each kind weighs.

**Where the default figures come from.** Not from guesswork. Three of the five
tasks send a prompt whose size the code itself bounds — `MAX_CONTENT_CHARS = 6000`
for an analysis, `MAX_LISTING_CHARS = 2500` for the set passes — so their prompts
were built and measured, and the seeds below are those measurements at roughly four
characters per token. They are therefore defensible before a single call is made.

**The two that are not.** An image's token weight depends on its resolution
through a formula no provider publishes, and the number of pages in a scan is
unknowable without opening it. Those two seeds are frank guesses, marked as such,
and the estimate says so out loud rather than presenting a number it cannot back.

**Which is why history beats all of it.** Once a run has happened, `usage_meter`
has recorded what each task actually consumed on *this user's* documents, at
*their* camera's resolution. From then on the forecast uses those averages and
stops guessing. The first run is coarse; every one after it is calibrated. That
progression is stated in the output, so an early estimate is never mistaken for a
precise one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from procrafiler.ai_estimate import AICallEstimate  # type: ignore[reportMissingImports]
from procrafiler.ai_naming import task_chain_from_env  # type: ignore[reportMissingImports]
from procrafiler.pricing import PriceTable, format_amount, load_price_table  # type: ignore[reportMissingImports]


@dataclass(frozen=True)
class TaskProfile:
    """What one call of a given task weighs."""

    tokens_in: float = 0.0
    tokens_out: float = 0.0
    pages: float = 0.0


# Measured by building the real prompts (see the module docstring); a token is
# taken as ~4 characters, the usual approximation for Latin-script text.
#   ANALYSIS  1 865 tokens for the rules alone, 3 441 with a full 6 000-char
#             document — the midpoint is used, since most documents are shorter.
#   NAMING /  781 tokens for a single-document set, ~73 more per extra document;
#   ORGANIZE  a five-document set measures ~1 060.
DEFAULT_PROFILES: dict[str, TaskProfile] = {
    "ANALYSIS": TaskProfile(tokens_in=2650, tokens_out=300),
    # Reading a transcript and naming a handful of moments — one short JSON reply.
    "VIDEO": TaskProfile(tokens_in=2000, tokens_out=200),
    "NAMING": TaskProfile(tokens_in=1060, tokens_out=250),
    "ORGANIZE": TaskProfile(tokens_in=1060, tokens_out=250),
    # Frank guesses — see COARSE_TASKS.
    "IMAGE": TaskProfile(tokens_in=1500, tokens_out=350),
    "OCR": TaskProfile(pages=2),
}

# The two seeds no measurement backs. Named so the output can admit it, and so a
# reader of this file is not left thinking every number here is equally solid.
COARSE_TASKS = frozenset({"IMAGE", "OCR"})

# Below this many recorded calls, an average says more about one odd document than
# about the user's corpus, and the seed is the steadier answer.
MIN_CALLS_TO_CALIBRATE = 4


@dataclass(frozen=True)
class CostForecast:
    table: PriceTable
    low: float = 0.0
    high: float = 0.0
    calibrated_tasks: frozenset[str] = frozenset()
    coarse_tasks: frozenset[str] = frozenset()
    unpriced_models: frozenset[str] = frozenset()
    billed_calls_low: int = 0
    billed_calls_high: int = 0

    @property
    def is_free(self) -> bool:
        return self.billed_calls_high == 0

    @property
    def is_complete(self) -> bool:
        """False when at least one billable model has no price: the figure is then
        a floor, not an estimate, and must never be shown as the total."""
        return not self.unpriced_models


def _history_path(paths) -> Path | None:  # noqa: ANN001 - RuntimePaths, avoids a cycle
    log_file = getattr(paths, "actions_log_file", None)
    return log_file if isinstance(log_file, Path) else None


def profiles_from_history(paths, *, min_calls: int = MIN_CALLS_TO_CALIBRATE) -> dict[str, TaskProfile]:  # noqa: ANN001
    """Average consumption per task, from what previous runs actually used.

    Reads the `run_ai_usage` events `usage_meter` writes. Providers are merged on
    purpose: token counts differ between tokenizers, but the point is the size of
    *the user's documents*, and someone switching provider still benefits from
    knowing their scans are long. Price is applied afterwards, per model.
    """
    log_file = _history_path(paths)
    if log_file is None or not log_file.is_file():
        return {}

    totals: dict[str, list[float]] = {}
    try:
        with log_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # a truncated tail must not hide the rest
                if not isinstance(event, dict) or event.get("action") != "run_ai_usage":
                    continue
                for row in event.get("ai_usage") or []:
                    if not isinstance(row, dict):
                        continue
                    task = str(row.get("task") or "")
                    calls = _as_number(row.get("calls"))
                    # A call whose provider reported nothing would drag every
                    # average towards zero and quietly halve the forecast.
                    measured = calls - _as_number(row.get("unmeasured_calls"))
                    if not task or measured <= 0:
                        continue
                    bucket = totals.setdefault(task, [0.0, 0.0, 0.0, 0.0])
                    bucket[0] += measured
                    bucket[1] += _as_number(row.get("tokens_in"))
                    bucket[2] += _as_number(row.get("tokens_out"))
                    bucket[3] += _as_number(row.get("pages"))
    except OSError:
        return {}

    return {
        task: TaskProfile(
            tokens_in=tokens_in / calls, tokens_out=tokens_out / calls, pages=pages / calls
        )
        for task, (calls, tokens_in, tokens_out, pages) in totals.items()
        if calls >= min_calls
    }


def _as_number(value) -> float:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def forecast_cost(
    estimate: AICallEstimate,
    *,
    paths=None,  # noqa: ANN001
    table: PriceTable | None = None,
    profiles: dict[str, TaskProfile] | None = None,
) -> CostForecast | None:
    """Price `estimate`. None when no table is available at all — in which case the
    caller must say cost is unknown, never zero."""
    price_table = table if table is not None else load_price_table(_config_dir(paths))
    if price_table is None:
        return None

    measured = profiles if profiles is not None else (profiles_from_history(paths) if paths else {})

    low = high = 0.0
    calibrated: set[str] = set()
    coarse: set[str] = set()
    unpriced: set[str] = set()
    calls_low = calls_high = 0

    for task, (task_low, task_high) in estimate.calls_by_task().items():
        if task_high <= 0:
            continue
        if task == "TRANSCRIBE":
            # The only line here that is not an estimate. ffprobe gave the exact
            # duration and Voxtral bills by duration, so this is arithmetic, not a
            # forecast — no profile, no calibration, no margin of error.
            chain = task_chain_from_env(task)
            if not chain:
                continue
            model = chain[0].model
            amount = price_table.cost(model, audio_seconds=estimate.audio_seconds)
            if amount is None:
                if chain[0].provider != "ollama":
                    unpriced.add(model)
                continue
            calls_low += task_low
            calls_high += task_high
            low += amount
            high += amount
            continue
        chain = task_chain_from_env(task)
        if not chain:
            continue
        model = chain[0].model
        price = price_table.price_for(model)
        if price is None or not price.is_priceable:
            # Not billable at all (a local model is simply absent from the table)
            # versus billable but unknown — only the second is a problem, and only
            # the second is reported.
            if chain[0].provider != "ollama":
                unpriced.add(model)
            continue

        calls_low += task_low
        calls_high += task_high
        profile = measured.get(task)
        if profile is not None:
            calibrated.add(task)
        else:
            profile = DEFAULT_PROFILES.get(task, TaskProfile())
            if task in COARSE_TASKS:
                coarse.add(task)

        for count, bucket in ((task_low, "low"), (task_high, "high")):
            amount = price_table.cost(
                model,
                tokens_in=int(profile.tokens_in * count),
                tokens_out=int(profile.tokens_out * count),
                pages=int(profile.pages * count),
            ) or 0.0
            if bucket == "low":
                low += amount
            else:
                high += amount

    return CostForecast(
        table=price_table,
        low=low,
        high=high,
        calibrated_tasks=frozenset(calibrated),
        coarse_tasks=frozenset(coarse),
        unpriced_models=frozenset(unpriced),
        billed_calls_low=calls_low,
        billed_calls_high=calls_high,
    )


def _config_dir(paths):  # noqa: ANN001
    """Where the user's own `pricing.json` would sit — alongside `settings.json`,
    the directory they already know as this app's configuration."""
    settings = getattr(paths, "settings_file", None)
    return settings.parent if isinstance(settings, Path) else None


def max_run_cost() -> float | None:
    """The ceiling past which a run asks before starting, from
    `PROCRAFILER_MAX_RUN_COST` (in the price table's currency). Unset = no ceiling.

    A ceiling is worth having even on an imprecise estimate, because the two
    failure modes are not symmetric: being asked needlessly costs a keystroke,
    spending silently costs money.
    """
    raw = os.environ.get("PROCRAFILER_MAX_RUN_COST", "").strip()
    if not raw:
        return None
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def format_cost_forecast(forecast: CostForecast | None) -> str:
    if forecast is None:
        return "Estimated cost: unavailable (no price table)."
    if forecast.is_free:
        return "Estimated cost: nothing — every configured task runs locally."

    table = forecast.table
    if forecast.low >= forecast.high:
        amount = f"≈ {format_amount(forecast.high, table)}"
    else:
        amount = f"≈ {format_amount(forecast.low, table)} to {format_amount(forecast.high, table)}"

    line = f"Estimated cost: {amount} {table.currency} (rates of {table.as_of()})"
    if not forecast.is_complete:
        line += (
            f" — AT LEAST, no price known for {', '.join(sorted(forecast.unpriced_models))}"
        )
    line += "."

    notes: list[str] = []
    if forecast.coarse_tasks:
        what = " and ".join(
            {"IMAGE": "images", "OCR": "scanned pages"}.get(task, task.lower())
            for task in sorted(forecast.coarse_tasks)
        )
        notes.append(f"the cost of {what} is a rough default until a first run measures yours")
    if forecast.calibrated_tasks:
        notes.append(f"calibrated on your previous runs for {', '.join(sorted(forecast.calibrated_tasks))}")
    if table.is_stale():
        age = table.age_days()
        notes.append(f"these rates are {age} days old — check {table.source or 'the provider'}")
    if notes:
        line += " (" + "; ".join(notes) + ".)"
    return line
