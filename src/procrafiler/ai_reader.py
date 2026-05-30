"""AI content readers for files that can't be read locally.

`content_reader` handles what we can read on disk (text files, readable PDFs).
This module reads the rest with an AI: scanned PDFs go through Mistral OCR, and
images go through a Mistral vision model. The extracted text then feeds the
same naming + classification steps as any other content — the AI reader just
turns image-based bytes into text.

Like the other AI tasks, the provider/model is never hardcoded: the OCR chain
is read from `PROCRAFILER_AI_OCR_PRIMARY` / `_FALLBACK`, and the vision chain
from `PROCRAFILER_AI_IMAGE_PRIMARY` / `_FALLBACK`. With no chain configured, or
on failure, the read falls back to "no text" and the caller routes the file to
manual review.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from procrafiler.ai_naming import (  # type: ignore[reportMissingImports]
    MISTRAL_CHAT_URL,
    ChainEntry,
    ProviderCallError,
    RateLimitedError,
    _extract_mistral_content,
    _mistral_is_rate_limited,
    _post_json,
    _safe_json_loads,
    _task_retries_from_env,
    _task_timeout_from_env,
    task_chain_from_env,
)

MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"

# OCR is slower than a chat completion; give it more room by default.
_DEFAULT_OCR_TIMEOUT = 120
_DEFAULT_VISION_TIMEOUT = 90

# Ask the vision model for usable text, not chit-chat: transcription first so
# scanned/photographed documents become classifiable, plus a short description.
_DEFAULT_VISION_PROMPT = (
    "Transcris fidèlement tout le texte visible dans cette image, puis décris "
    "brièvement ce qu'elle représente. Réponds en français, en texte brut. "
    "Si c'est un document, restitue les informations clés (émetteur, type, "
    "date, montants)."
)

_IMAGE_SUFFIX_TO_MIME = {
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
    "bmp": "bmp",
    "tif": "tiff",
    "tiff": "tiff",
    "heic": "heic",
    "heif": "heif",
}


def _image_mime(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return f"image/{_IMAGE_SUFFIX_TO_MIME.get(suffix, 'jpeg')}"


@dataclass(frozen=True)
class AIReadResult:
    text: str | None
    provider: str
    model: str
    used_fallback: bool
    reason: str | None


def _extract_ocr_text(body: dict[str, Any]) -> str:
    pages = body.get("pages")
    if not isinstance(pages, list):
        raise ProviderCallError(f"OCR_BAD_RESPONSE_SHAPE: {body}")
    parts: list[str] = []
    for page in pages:
        if isinstance(page, dict):
            markdown = page.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                parts.append(markdown)
    return "\n\n".join(parts).strip()


def call_mistral_ocr(path: Path, model: str, timeout: int = _DEFAULT_OCR_TIMEOUT) -> str:
    """Run Mistral OCR on a local PDF and return the extracted text.

    The file is sent inline as a base64 data URI (no separate Files upload).
    """
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise ProviderCallError("MISTRAL_API_KEY is not set")

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{encoded}",
        },
    }

    status_code, raw_content = _post_json(
        MISTRAL_OCR_URL,
        payload=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )

    body = _safe_json_loads(raw_content)
    if _mistral_is_rate_limited(status_code, body):
        raise RateLimitedError("RATE_LIMITED")
    if status_code >= 400:
        raise ProviderCallError(f"OCR_API_ERROR_{status_code}: {body}")

    return _extract_ocr_text(body)


def read_with_ocr(
    path: Path,
    *,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> AIReadResult:
    """Read a scanned/image PDF via the configured OCR chain.

    Returns an AIReadResult whose `text` is None when no chain is configured,
    when every provider fails, or when OCR yields nothing — in which case the
    caller must route the file to manual review.
    """
    chain_entries = chain if chain is not None else task_chain_from_env("OCR")
    if not chain_entries:
        return AIReadResult(text=None, provider="none", model="none", used_fallback=True, reason="chain_not_configured")

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("OCR", default_value=_DEFAULT_OCR_TIMEOUT)
    retry_count = retries if retries is not None else _task_retries_from_env("OCR", default_value=2)

    last_error = "unknown"
    for entry in chain_entries:
        for attempt in range(retry_count + 1):
            try:
                if entry.provider == "mistral":
                    text = call_mistral_ocr(path, entry.model, timeout=timeout)
                else:
                    raise ProviderCallError(f"unsupported_ocr_provider:{entry.provider}")

                if not text.strip():
                    raise ProviderCallError("OCR_EMPTY_RESULT")
                return AIReadResult(
                    text=text, provider=entry.provider, model=entry.model, used_fallback=False, reason=None
                )
            except (RateLimitedError, ProviderCallError) as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    sleep_fn(2**attempt)
                    continue
                break

    return AIReadResult(text=None, provider="fallback", model="fallback", used_fallback=True, reason=last_error)


def call_mistral_vision(
    path: Path,
    model: str,
    prompt: str = _DEFAULT_VISION_PROMPT,
    timeout: int = _DEFAULT_VISION_TIMEOUT,
) -> str:
    """Send an image to a Mistral vision model and return its text output.

    Uses chat completions with the image inlined as a base64 data URI.
    """
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise ProviderCallError("MISTRAL_API_KEY is not set")

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    data_uri = f"data:{_image_mime(path)};base64,{encoded}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": data_uri},
                ],
            }
        ],
    }

    status_code, raw_content = _post_json(
        MISTRAL_CHAT_URL,
        payload=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )

    body = _safe_json_loads(raw_content)
    if _mistral_is_rate_limited(status_code, body):
        raise RateLimitedError("RATE_LIMITED")
    if status_code >= 400:
        raise ProviderCallError(f"VISION_API_ERROR_{status_code}: {body}")

    return _extract_mistral_content(body)


def read_with_vision(
    path: Path,
    *,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> AIReadResult:
    """Read an image via the configured vision chain (`PROCRAFILER_AI_IMAGE_*`).

    Returns an AIReadResult whose `text` is None when no chain is configured,
    when every provider fails, or when the model returns nothing — in which
    case the caller must route the file to manual review.
    """
    chain_entries = chain if chain is not None else task_chain_from_env("IMAGE")
    if not chain_entries:
        return AIReadResult(text=None, provider="none", model="none", used_fallback=True, reason="chain_not_configured")

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("IMAGE", default_value=_DEFAULT_VISION_TIMEOUT)
    retry_count = retries if retries is not None else _task_retries_from_env("IMAGE", default_value=2)

    last_error = "unknown"
    for entry in chain_entries:
        for attempt in range(retry_count + 1):
            try:
                if entry.provider == "mistral":
                    text = call_mistral_vision(path, entry.model, timeout=timeout)
                else:
                    raise ProviderCallError(f"unsupported_vision_provider:{entry.provider}")

                if not text.strip():
                    raise ProviderCallError("VISION_EMPTY_RESULT")
                return AIReadResult(
                    text=text, provider=entry.provider, model=entry.model, used_fallback=False, reason=None
                )
            except (RateLimitedError, ProviderCallError) as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    sleep_fn(2**attempt)
                    continue
                break

    return AIReadResult(text=None, provider="fallback", model="fallback", used_fallback=True, reason=last_error)
