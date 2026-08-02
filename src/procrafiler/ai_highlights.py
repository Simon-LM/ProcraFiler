"""Reading a transcript to decide where to *look*.

This is the pass that makes the whole design pay off. Vision is the expensive
part, so instead of sampling a video on a blind grid we ask a cheap text model a
narrow question: given what was said and when, which few moments would show what
this recording actually is?

**It is asked for a filing decision, not a summary.** The moments that matter here
are the ones where something is shown, named, or established — a subject entering
frame, a document held up, a location stated. A model asked to "find the
interesting parts" returns narrative highlights; asked to find where the *subject*
becomes visible, it returns something a filing system can use.

It never fails the read. A missing chain, an unparseable answer, a transcript with
no speech — every one of them returns an empty list, and the sampler falls back to
an even spread. Choosing *where* to look is an optimisation; being able to look at
all is not.
"""

from __future__ import annotations

import time
from typing import Any

from procrafiler.ai_naming import (  # type: ignore[reportMissingImports]
    ChainEntry,
    ProviderCallError,
    RateLimitedError,
    _extract_json_dict,
    _task_retries_from_env,
    _task_timeout_from_env,
    call_mistral_chat,
    call_ollama_chat,
    task_chain_from_env,
)
from procrafiler.ai_transcribe import TranscriptResult, format_transcript  # type: ignore[reportMissingImports]
from procrafiler.frame_sampling import Highlight  # type: ignore[reportMissingImports]

MAX_TRANSCRIPT_CHARS = 6000


def _build_highlight_prompt(transcript: str, duration_seconds: float, wanted: int) -> str:
    return (
        "Below is a timestamped transcript of a recording. Your job is NOT to summarise it.\n"
        "Choose the moments where a still image taken from the video would best show WHAT "
        "THIS RECORDING IS, so it can be filed correctly.\n\n"
        "Prefer moments where something is shown, named, established or demonstrated: a "
        "subject appearing, a document or object held up, a place identified, damage or work "
        "being pointed at. Avoid moments that are only talk with nothing to see.\n"
        f"Pick at most {wanted}, spread across the recording rather than clustered in one "
        "passage. Fewer is fine if the recording does not warrant more.\n\n"
        f"Total duration: {duration_seconds:.0f} seconds.\n\n"
        "Return JSON only, with this exact schema:\n"
        "{\n"
        '  "moments": [\n'
        '    {"start": <seconds, number>, "end": <seconds, number>, "why": "<a few words>"}\n'
        "  ]\n"
        "}\n"
        "`start` and `end` are seconds from the beginning, as numbers, never a clock string. "
        "They must fall inside the duration above.\n\n"
        "TRANSCRIPT:\n"
        f"{transcript}"
    )


def _parse_moments(payload: dict[str, Any], duration_seconds: float, wanted: int) -> list[Highlight]:
    raw_moments = payload.get("moments")
    if not isinstance(raw_moments, list):
        return []
    highlights: list[Highlight] = []
    for raw in raw_moments:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start"))
        except (TypeError, ValueError):
            continue
        try:
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            end = start
        # A model that invents a timestamp past the end would send ffmpeg seeking
        # into nothing and cost us a frame for a black image.
        if start < 0 or start > duration_seconds:
            continue
        highlights.append(
            Highlight(
                start=start,
                end=min(max(end, start), duration_seconds),
                reason=str(raw.get("why") or "").strip()[:80],
            )
        )
        if len(highlights) >= wanted:
            break
    return highlights


def select_highlights(
    transcript: TranscriptResult,
    duration_seconds: float,
    *,
    wanted: int,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> list[Highlight]:
    """Moments worth a still, in the model's order of preference.

    The order is load-bearing: the sampler spends its budget down this list, so
    the best moment is the one most likely to survive the spacing rule.
    """
    if wanted <= 0 or duration_seconds <= 0 or not transcript.has_speech:
        return []
    chain_entries = chain if chain is not None else task_chain_from_env("VIDEO")
    if not chain_entries:
        return []

    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _task_timeout_from_env("VIDEO", default_value=60, provider=chain_entries[0].provider)
    )
    retry_count = retries if retries is not None else _task_retries_from_env("VIDEO", default_value=1)
    prompt = _build_highlight_prompt(
        format_transcript(transcript, max_chars=MAX_TRANSCRIPT_CHARS), duration_seconds, wanted
    )

    for entry in chain_entries:
        for attempt in range(retry_count + 1):
            try:
                if entry.provider == "mistral":
                    raw_output = call_mistral_chat(prompt, entry.model, timeout=timeout, task="VIDEO")
                elif entry.provider == "ollama":
                    raw_output = call_ollama_chat(prompt, entry.model, timeout=timeout, task="VIDEO")
                else:
                    raise ProviderCallError(f"unsupported_provider:{entry.provider}")
                payload = _extract_json_dict(raw_output)
                if payload is None:
                    raise ProviderCallError("INVALID_JSON_RESPONSE")
                return _parse_moments(payload, duration_seconds, wanted)
            except (RateLimitedError, ProviderCallError):
                if attempt < retry_count:
                    sleep_fn(2 ** attempt)
    # Every provider failed. An even spread is a worse plan than a chosen one, but
    # it is a perfectly usable one — this must not abort the read.
    return []
