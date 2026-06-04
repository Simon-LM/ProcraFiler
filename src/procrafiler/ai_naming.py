# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Shared AI provider plumbing (chains, HTTP calls, JSON extraction).

This module is the low-level layer every AI task builds on: parsing the
per-task provider chain from the environment, calling Mistral / Ollama with
retry-able errors, and pulling a JSON object out of a noisy model reply. It is
deliberately task-agnostic — the actual tasks live elsewhere (`ai_analysis` for
the unified read→name→classify→summarize call, `ai_reader` for OCR/vision).

The module keeps its historical name `ai_naming` only to avoid churning the
imports of its several consumers; it no longer contains naming-specific logic.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"


@dataclass(frozen=True)
class ChainEntry:
    provider: str
    model: str


class RateLimitedError(RuntimeError):
    pass


class ProviderCallError(RuntimeError):
    pass


SUPPORTED_AI_TASKS: tuple[str, ...] = (
    "ANALYSIS",
    "OCR",
    "PDF",
    "IMAGE",
    "VIDEO",
    "SUPERVISOR",
)


def parse_provider_chain(chain_raw: str) -> list[ChainEntry]:
    entries: list[ChainEntry] = []
    for chunk in chain_raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        if ":" not in token:
            continue
        provider, model = token.split(":", 1)
        provider = provider.strip().lower()
        model = model.strip()
        if not provider or not model:
            continue
        entries.append(ChainEntry(provider=provider, model=model))
    return entries


def task_chain_from_env(task: str) -> list[ChainEntry]:
    task_key = task.strip().upper()
    if task_key not in SUPPORTED_AI_TASKS:
        return []

    primary = os.environ.get(f"PROCRAFILER_AI_{task_key}_PRIMARY", "")
    fallback = os.environ.get(f"PROCRAFILER_AI_{task_key}_FALLBACK", "")
    chain = [*parse_provider_chain(primary), *parse_provider_chain(fallback)]
    return chain


def _task_timeout_from_env(task: str, default_value: int = 60) -> int:
    task_key = task.strip().upper()
    task_value = os.environ.get(f"PROCRAFILER_AI_{task_key}_TIMEOUT")
    global_value = os.environ.get("PROCRAFILER_AI_TIMEOUT")

    for raw in (task_value, global_value):
        if raw is None or not raw.strip():
            continue
        try:
            parsed = int(raw)
        except ValueError:
            continue
        if parsed > 0:
            return parsed

    return default_value


def _task_retries_from_env(task: str, default_value: int = 2) -> int:
    task_key = task.strip().upper()
    task_value = os.environ.get(f"PROCRAFILER_AI_{task_key}_RETRIES")
    global_value = os.environ.get("PROCRAFILER_AI_RETRIES")

    for raw in (task_value, global_value):
        if raw is None or not raw.strip():
            continue
        try:
            parsed = int(raw)
        except ValueError:
            continue
        if parsed >= 0:
            return parsed

    return default_value


def _safe_json_loads(content: bytes) -> dict[str, Any]:
    if not content:
        return {}
    try:
        loaded = json.loads(content.decode("utf-8"))
    except Exception:
        return {}
    if isinstance(loaded, dict):
        return cast(dict[str, Any], loaded)
    return {}


def _mistral_is_rate_limited(status_code: int, body: dict[str, Any]) -> bool:
    if status_code == 429:
        return True
    return body.get("object") == "error" and body.get("type") == "rate_limited"


def _extract_mistral_content(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except Exception as exc:
        raise ProviderCallError(f"BAD_RESPONSE_SHAPE: {exc}") from exc

    if isinstance(content, list):
        text_blocks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_blocks.append(str(block.get("text", "")))
        return "\n".join(text_blocks).strip()
    return str(content).strip()


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except HTTPError as err:
        return err.code, err.read()
    except OSError as err:
        # Any network-level failure — URLError, a socket read/connect TimeoutError
        # (a slow vision/OCR call), connection reset, etc. — becomes a retryable
        # provider error so the caller's retry + failover handles it gracefully
        # instead of letting it crash the whole batch.
        raise ProviderCallError(f"NETWORK_ERROR: {err}") from err


def call_mistral_chat(prompt: str, model: str, timeout: int = 60, **api_params: Any) -> str:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise ProviderCallError("MISTRAL_API_KEY is not set")

    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "model": model,
    }
    payload.update(api_params)

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
        raise ProviderCallError(f"API_ERROR_{status_code}: {body}")

    return _extract_mistral_content(body)


def call_ollama_chat(prompt: str, model: str, timeout: int = 60) -> str:
    status_code, raw_content = _post_json(
        OLLAMA_CHAT_URL,
        payload={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    body = _safe_json_loads(raw_content)

    if status_code >= 400:
        raise ProviderCallError(f"OLLAMA_ERROR_{status_code}: {body}")

    try:
        content = body["message"]["content"]
    except Exception as exc:
        raise ProviderCallError(f"OLLAMA_BAD_RESPONSE: {exc}") from exc
    return str(content).strip()


def _extract_json_dict(raw_output: str) -> dict[str, Any] | None:
    text = raw_output.strip()
    if not text:
        return None

    # First try direct JSON parsing.
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return cast(dict[str, Any], loaded)
    except Exception:
        pass

    # Then try fenced JSON blocks.
    fenced_blocks = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    for block in fenced_blocks:
        try:
            loaded = json.loads(block)
            if isinstance(loaded, dict):
                return cast(dict[str, Any], loaded)
        except Exception:
            continue

    # Finally, extract the first decodable JSON object in noisy text.
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        idx = match.start()
        try:
            loaded, _end = decoder.raw_decode(text[idx:])
            if isinstance(loaded, dict):
                return cast(dict[str, Any], loaded)
        except Exception:
            continue

    return None
