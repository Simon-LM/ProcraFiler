"""Turning speech into text with timestamps — the cheap half of reading a video.

Mistral has no video model, and that turns out to be an advantage rather than a
gap. Transcription is billed **per second of audio**; vision is billed per image,
by pixel count. Speech is therefore an order of magnitude cheaper per minute of
recording than looking at it, and it is also the part that carries the *meaning*.
So we listen first, cheaply, and only then spend on looking — at the few moments
listening told us mattered.

The response contract was verified against the live API rather than inferred:

```json
{"text": "…", "language": null,
 "segments": [{"type": "transcription_segment", "text": " …",
               "start": 0.9, "end": 3.1, "speaker_id": null}],
 "usage": {"prompt_audio_seconds": 7}}
```

Two behaviours confirmed the same way, both of which the caller must handle as
ordinary outcomes rather than failures:

- **audio with no speech** (music, a tone, room noise) returns HTTP 200 with
  `text: ""` and `segments: []`. It is not an error, and treating it as one would
  send perfectly good home videos to manual review.
- `usage.prompt_audio_seconds` is the **billed unit**, whole seconds.

`timestamp_granularities` is deliberately not combined with `language`: the API
rejects the pair, and timestamps are what the whole design depends on.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from procrafiler.ai_naming import (  # type: ignore[reportMissingImports]
    ChainEntry,
    ProviderCallError,
    RateLimitedError,
    _mistral_is_rate_limited,
    _safe_json_loads,
    _task_retries_from_env,
    _task_timeout_from_env,
    task_chain_from_env,
)
from procrafiler.usage_meter import record_response  # type: ignore[reportMissingImports]

MISTRAL_TRANSCRIPTION_URL = "https://api.mistral.ai/v1/audio/transcriptions"

_DEFAULT_TRANSCRIBE_TIMEOUT = 600


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    text: str = ""
    segments: list[TranscriptSegment] = field(default_factory=list)
    audio_seconds: int = 0
    provider: str = "none"
    model: str = "none"
    reason: str | None = None

    @property
    def has_speech(self) -> bool:
        """Empty text is a legitimate answer — silence, music, or wind. The caller
        falls back to looking rather than giving up."""
        return bool(self.text.strip())


def _multipart(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    """Build a multipart/form-data body.

    Hand-rolled rather than pulling in `requests`: it is thirty lines, it keeps the
    dependency footprint of an app that files personal documents small, and the
    rest of this codebase already speaks `urllib` directly.
    """
    boundary = f"----procrafiler{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{file_path.name}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return (b"".join(parts), f"multipart/form-data; boundary={boundary}")


def _post_multipart(url: str, body: bytes, content_type: str, api_key: str, timeout: int) -> tuple[int, bytes]:
    request = Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Content-Type", content_type)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as err:
        return err.code, err.read()
    except OSError as err:
        raise ProviderCallError(f"NETWORK_ERROR: {err}") from err


def _parse_segments(body: dict[str, Any]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for raw in body.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start"))
            end = float(raw.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        segments.append(TranscriptSegment(start=start, end=max(end, start), text=text))
    return segments


def call_mistral_transcription(
    path: Path, model: str, timeout: int = _DEFAULT_TRANSCRIBE_TIMEOUT, *, task: str = "TRANSCRIBE"
) -> TranscriptResult:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise ProviderCallError("MISTRAL_API_KEY is not set")

    body_bytes, content_type = _multipart(
        # No `language`: the API refuses it together with timestamp_granularities,
        # and the timestamps are the point. Voxtral detects the language anyway.
        {"model": model, "timestamp_granularities": "segment"},
        "file",
        path,
    )
    status_code, raw = _post_multipart(
        MISTRAL_TRANSCRIPTION_URL, body_bytes, content_type, api_key, timeout
    )
    body = _safe_json_loads(raw)
    if _mistral_is_rate_limited(status_code, body):
        raise RateLimitedError("RATE_LIMITED")
    if status_code >= 400:
        raise ProviderCallError(f"TRANSCRIBE_API_ERROR_{status_code}: {body}")

    record_response("mistral", model, task, body)
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    try:
        audio_seconds = int(usage.get("prompt_audio_seconds") or 0)
    except (TypeError, ValueError):
        audio_seconds = 0

    return TranscriptResult(
        text=str(body.get("text") or "").strip(),
        segments=_parse_segments(body),
        audio_seconds=audio_seconds,
        provider="mistral",
        model=model,
    )


def transcribe(
    path: Path,
    *,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> TranscriptResult:
    """Transcribe an audio file through the configured TRANSCRIBE chain.

    Returns a result with `reason` set rather than raising: a video whose audio
    could not be transcribed is still worth looking at, so a failure here must
    degrade the reading, never end it.
    """
    chain_entries = chain if chain is not None else task_chain_from_env("TRANSCRIBE")
    if not chain_entries:
        return TranscriptResult(reason="chain_not_configured")
    if not path.is_file() or path.stat().st_size == 0:
        return TranscriptResult(reason="no_audio_extracted")

    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _task_timeout_from_env(
            "TRANSCRIBE", default_value=_DEFAULT_TRANSCRIBE_TIMEOUT, provider=chain_entries[0].provider
        )
    )
    retry_count = retries if retries is not None else _task_retries_from_env("TRANSCRIBE", default_value=2)

    last_error = "unknown"
    for entry in chain_entries:
        for attempt in range(retry_count + 1):
            try:
                if entry.provider != "mistral":
                    # Ollama exposes no transcription endpoint. Saying so plainly
                    # beats a confusing network error on a misconfigured chain.
                    raise ProviderCallError(f"unsupported_transcribe_provider:{entry.provider}")
                return call_mistral_transcription(path, entry.model, timeout=timeout)
            except RateLimitedError as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    sleep_fn(2 ** attempt)
            except ProviderCallError as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    sleep_fn(2 ** attempt)
    return TranscriptResult(reason=f"transcription_failed:{last_error}")


def rescale_to_source_time(result: TranscriptResult, speed: float) -> TranscriptResult:
    """Put the timestamps back onto the ORIGINAL recording's clock.

    The audio is sped up before being sent, because transcription is billed by the
    second submitted. The transcript therefore comes back on the sped-up clock: a
    passage the model reports at 100 s of a 1.25x file happened at 125 s of the
    real recording.

    Nothing else in the chain knows that. The frame planner takes these timestamps
    at face value and hands them to ffmpeg, so forgetting this multiplication does
    not raise, does not fail a test, and does not look wrong in a log — it silently
    extracts every still from the wrong moment of the film, drifting further the
    later the passage. That is why the conversion is a named function with its own
    tests rather than a `* speed` buried in the reader.

    `audio_seconds` is deliberately NOT rescaled: it is what the provider billed,
    and the bill is for the shortened audio that was actually sent.
    """
    if speed == 1.0 or not result.segments:
        return result
    return TranscriptResult(
        text=result.text,
        segments=[
            TranscriptSegment(start=segment.start * speed, end=segment.end * speed, text=segment.text)
            for segment in result.segments
        ],
        audio_seconds=result.audio_seconds,
        provider=result.provider,
        model=result.model,
        reason=result.reason,
    )


def format_transcript(result: TranscriptResult, *, max_chars: int = 6000) -> str:
    """The transcript as it goes to the analysis, timestamped.

    Timestamps are kept in the text on purpose: they let the analysis quote *when*
    something was said, and they are what the highlight pass reasons over.
    """
    if not result.segments:
        return result.text[:max_chars]
    lines: list[str] = []
    used = 0
    for segment in result.segments:
        line = f"[{_clock(segment.start)}] {segment.text}"
        if used + len(line) > max_chars:
            lines.append("[…transcript truncated…]")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _clock(seconds: float) -> str:
    total = int(max(0.0, seconds))
    if total >= 3600:
        return f"{total // 3600:d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60:d}:{total % 60:02d}"
