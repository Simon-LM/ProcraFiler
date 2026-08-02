"""What a run actually consumed — measured, not guessed.

Every provider response already carries what the call cost: Mistral chat returns a
`usage` block, Mistral OCR a `usage_info` one, Ollama its `prompt_eval_count` /
`eval_count`. Until now all of it was parsed off the wire and thrown away, so the
only thing the app could say about cost was a *count of calls* — which says nothing
about money, and counted a free local call exactly like a paid one.

This module keeps that information. It answers two different questions:

**After a run** — what did it really consume, per task and per model. That is a
measurement, not an estimate, and it is the honest number to show the user.

**Before a future run** — the same history is what makes a forecast possible at
all. The tokens an image costs depend on its resolution through a formula we do not
know; measuring a few real photos from *this* user's own camera beats any published
formula. The estimator can then be calibrated on observation instead of guesswork.

Two deliberate non-goals:

- **No prices here.** This module counts tokens and pages, never euros. Prices
  change, and a figure baked into the source would go stale silently in everyone's
  installation. Conversion belongs elsewhere, against a dated table.
- **Never breaks a call.** Accounting is strictly secondary to the work. Every
  entry point swallows its own errors: an unknown response shape costs us the
  numbers for that call, recorded as *unmeasured*, and nothing else. A run must
  never fail because the bookkeeping did.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

# Providers that bill. Anything else — Ollama today — runs on the user's own
# machine, so its calls are counted for transparency but are worth nothing.
_BILLED_PROVIDERS = {"mistral"}


@dataclass
class UsageEntry:
    """One (provider, model, task) triple, aggregated over a run."""

    provider: str
    model: str
    task: str
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    pages: int = 0
    # Transcription is billed by duration, not by tokens or pages — a third unit,
    # not a variant of the other two. Folding it into tokens would price an hour
    # of speech at a few cents' worth of text.
    audio_seconds: int = 0
    unmeasured_calls: int = 0

    @property
    def is_billed(self) -> bool:
        return self.provider in _BILLED_PROVIDERS

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.task)


class RunUsage:
    """Everything one run consumed. Aggregated per (provider, model, task) rather
    than kept call by call: a 200-file run would otherwise hold 600 records to say
    what three lines say."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], UsageEntry] = {}

    def add(
        self,
        *,
        provider: str,
        model: str,
        task: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        pages: int = 0,
        audio_seconds: int = 0,
        measured: bool = True,
    ) -> None:
        key = (provider or "unknown", model or "unknown", task or "unknown")
        entry = self._entries.get(key)
        if entry is None:
            entry = UsageEntry(provider=key[0], model=key[1], task=key[2])
            self._entries[key] = entry
        entry.calls += 1
        entry.tokens_in += max(0, tokens_in)
        entry.tokens_out += max(0, tokens_out)
        entry.pages += max(0, pages)
        entry.audio_seconds += max(0, audio_seconds)
        if not measured:
            entry.unmeasured_calls += 1

    def entries(self) -> list[UsageEntry]:
        """Billed first, then by descending token volume — the expensive lines are
        the ones the user needs to see, and they should not be buried."""
        return sorted(
            self._entries.values(),
            key=lambda e: (
                not e.is_billed,
                -(e.tokens_in + e.tokens_out),
                -e.pages,
                -e.audio_seconds,
                e.task,
            ),
        )

    @property
    def is_empty(self) -> bool:
        return not self._entries

    @property
    def total_calls(self) -> int:
        return sum(entry.calls for entry in self._entries.values())

    @property
    def billed_calls(self) -> int:
        return sum(entry.calls for entry in self._entries.values() if entry.is_billed)

    @property
    def local_calls(self) -> int:
        return self.total_calls - self.billed_calls

    @property
    def total_tokens(self) -> int:
        return sum(e.tokens_in + e.tokens_out for e in self._entries.values() if e.is_billed)

    @property
    def total_pages(self) -> int:
        return sum(entry.pages for entry in self._entries.values() if entry.is_billed)

    @property
    def total_audio_seconds(self) -> int:
        return sum(entry.audio_seconds for entry in self._entries.values() if entry.is_billed)


# A ContextVar, matching how `run_id` is threaded through the pipeline: recording
# happens deep inside the provider callers, and passing a meter down through every
# AI task signature would be noise that any new call site could silently forget.
# When no meter is active — a unit test, a one-off script — recording is a no-op.
_CURRENT_USAGE: ContextVar[RunUsage | None] = ContextVar("procrafiler_usage", default=None)


@contextmanager
def usage_scope(usage: RunUsage | None = None) -> Iterator[RunUsage]:
    meter = usage if usage is not None else RunUsage()
    token = _CURRENT_USAGE.set(meter)
    try:
        yield meter
    finally:
        _CURRENT_USAGE.reset(token)


def current_usage() -> RunUsage | None:
    return _CURRENT_USAGE.get()


def _as_int(value: Any) -> int:
    """Providers are not obliged to agree on types, and a string count must not
    poison the totals."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


@dataclass(frozen=True)
class CallUnits:
    """What one call consumed, in whichever units its endpoint bills by.

    A record rather than a tuple: providers keep adding billing units — tokens,
    then OCR pages, now seconds of audio — and each addition would otherwise
    change the shape of a value every caller unpacks positionally.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    pages: int = 0
    audio_seconds: int = 0
    measured: bool = False


def extract_units(body: Any) -> CallUnits:
    """Pull the billed units out of a provider response.

    Deliberately tolerant across the shapes we know and any we do not:

    - Mistral chat / vision — `usage: {prompt_tokens, completion_tokens}`
    - Mistral OCR — `usage_info: {pages_processed, ...}`; billed per page, not per
      token, so pages are a first-class unit here rather than a curiosity.
    - Mistral transcription — `usage: {prompt_audio_seconds}`; billed per second of
      audio. Its `prompt_tokens` is also present and is NOT the billing basis, so
      recording both is correct: the price table decides which one it charges for.
    - Ollama — `prompt_eval_count` / `eval_count` at the top level.

    `measured` is False when nothing recognisable was found. That flag matters: it
    is the difference between "this call was free" and "we do not know what this
    call cost", and collapsing the two would quietly understate a bill.
    """
    if not isinstance(body, dict):
        return CallUnits()

    tokens_in = tokens_out = pages = audio_seconds = 0
    found = False

    for container_key in ("usage", "usage_info"):
        container = body.get(container_key)
        if not isinstance(container, dict):
            continue
        for key in ("prompt_tokens", "input_tokens"):
            if key in container:
                tokens_in += _as_int(container[key])
                found = True
        for key in ("completion_tokens", "output_tokens"):
            if key in container:
                tokens_out += _as_int(container[key])
                found = True
        for key in ("pages_processed", "pages"):
            if key in container:
                pages += _as_int(container[key])
                found = True
        for key in ("prompt_audio_seconds", "audio_seconds"):
            if key in container:
                audio_seconds += _as_int(container[key])
                found = True

    # Ollama reports at the top level, with its own names.
    if "prompt_eval_count" in body:
        tokens_in += _as_int(body["prompt_eval_count"])
        found = True
    if "eval_count" in body:
        tokens_out += _as_int(body["eval_count"])
        found = True

    return CallUnits(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        pages=pages,
        audio_seconds=audio_seconds,
        measured=found,
    )


def record_response(provider: str, model: str, task: str, body: Any) -> None:
    """Account for one completed provider call. Never raises: bookkeeping must not
    be able to fail a run that otherwise succeeded."""
    meter = _CURRENT_USAGE.get()
    if meter is None:
        return
    try:
        units = extract_units(body)
        meter.add(
            provider=provider,
            model=model,
            task=task,
            tokens_in=units.tokens_in,
            tokens_out=units.tokens_out,
            pages=units.pages,
            audio_seconds=units.audio_seconds,
            measured=units.measured,
        )
    except Exception:  # noqa: BLE001 - accounting is never worth a crash
        pass


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _format_duration(seconds: int) -> str:
    """Audio is billed per second but read by humans in minutes."""
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def format_usage_report(usage: RunUsage) -> str:
    """The consumption of a finished run, in units — never in money.

    The wording says "consumed", not "cost": this app does not know today's prices,
    and the honest thing is to hand the user a number they can multiply themselves
    against the current published rate.
    """
    if usage.is_empty:
        return "AI usage: no provider call was made."

    lines = ["AI usage actually consumed by this run:"]
    for entry in usage.entries():
        where = "" if entry.is_billed else "  (local, free)"
        units: list[str] = []
        if entry.tokens_in or entry.tokens_out:
            units.append(
                f"{_format_count(entry.tokens_in)} in / {_format_count(entry.tokens_out)} out tokens"
            )
        if entry.pages:
            units.append(f"{_format_count(entry.pages)} page(s)")
        if entry.audio_seconds:
            units.append(f"{_format_duration(entry.audio_seconds)} of audio")
        if not units:
            units.append("volume not reported by the provider")
        detail = ", ".join(units)
        lines.append(
            f"  {entry.task:<9} {entry.provider}:{entry.model} — "
            f"{entry.calls} call(s), {detail}{where}"
        )
        if entry.unmeasured_calls:
            lines.append(
                f"    ! {entry.unmeasured_calls} of them reported no usage figures"
            )

    if usage.billed_calls:
        totals = [f"{_format_count(usage.total_tokens)} token(s)"]
        if usage.total_pages:
            totals.append(f"{_format_count(usage.total_pages)} OCR page(s)")
        if usage.total_audio_seconds:
            totals.append(f"{_format_duration(usage.total_audio_seconds)} of audio")
        lines.append(
            f"  Billable total: {usage.billed_calls} call(s), " + ", ".join(totals)
        )
    if usage.local_calls:
        lines.append(f"  Local calls (no cost): {usage.local_calls}")
    return "\n".join(lines)
