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
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from procrafiler.usage_meter import record_response  # type: ignore[reportMissingImports]

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
    "NAMING",
    "ORGANIZE",
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


# Timeouts are PROVIDER-AWARE, with two separate knobs:
#   - API (Mistral): `PROCRAFILER_AI_TIMEOUT` (moderate default, 60s) — the API is fast.
#   - Local (Ollama): `PROCRAFILER_AI_LOCAL_TIMEOUT` (generous default, 900s) — local
#     inference is far slower + varies with the machine and file size, so a merely-slow
#     call must not be killed and dropped to manual review.
# A per-task `PROCRAFILER_AI_<TASK>_TIMEOUT` overrides either (it's the most specific).
_LOCAL_PROVIDERS = frozenset({"ollama"})
_LOCAL_DEFAULT_TIMEOUT = 900


def _task_timeout_from_env(task: str, default_value: int = 60, *, provider: str | None = None) -> int:
    task_key = task.strip().upper()
    is_local = provider in _LOCAL_PROVIDERS
    provider_var = "PROCRAFILER_AI_LOCAL_TIMEOUT" if is_local else "PROCRAFILER_AI_TIMEOUT"

    # Precedence: per-task override (any provider) > the provider's own knob > default.
    for raw in (os.environ.get(f"PROCRAFILER_AI_{task_key}_TIMEOUT"), os.environ.get(provider_var)):
        if raw is None or not raw.strip():
            continue
        try:
            parsed = int(raw)
        except ValueError:
            continue
        if parsed > 0:
            return parsed

    if is_local:
        return max(default_value, _LOCAL_DEFAULT_TIMEOUT)
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


def _ai_sampling_params() -> dict[str, float]:
    """Sampling params for the Mistral chat calls, read from the environment.

    By DEFAULT nothing is sent → Mistral applies its own (neutral) default — the
    reference baseline. Set `PROCRAFILER_AI_TEMPERATURE` and/or
    `PROCRAFILER_AI_TOP_P` to a float to override GLOBALLY (e.g. compare a neutral
    run against 0.0 / 0.3 / 0.5 on the same inputs). A value that doesn't parse as
    a float is ignored (that param is simply not sent). An explicit param passed
    at the call site still wins, since it is applied after these.
    """
    params: dict[str, float] = {}
    for env_key, api_key in (
        ("PROCRAFILER_AI_TEMPERATURE", "temperature"),
        ("PROCRAFILER_AI_TOP_P", "top_p"),
    ):
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            continue
        try:
            params[api_key] = float(raw)
        except ValueError:
            pass
    return params


def _ai_throttle(sleep_fn: Any = time.sleep) -> None:
    """Optional pause before each real provider HTTP call.

    Set `PROCRAFILER_AI_THROTTLE` to a number of seconds to space out calls —
    useful when driving a LOCAL model (Ollama) to avoid overheating the GPU on
    a long sequential run. Default (unset / 0 / invalid) = no pause, so Mistral
    and production are unaffected. Mocked tests patch `_post_json`, so they never
    reach this — only real network calls are throttled.
    """
    raw = os.environ.get("PROCRAFILER_AI_THROTTLE", "")
    if not raw.strip():
        return
    try:
        seconds = float(raw)
    except ValueError:
        return
    if seconds > 0:
        sleep_fn(seconds)


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
    _ai_throttle()
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


def _post_ollama_chat_stream(
    url: str, payload: dict[str, Any], idle_timeout: int
) -> tuple[int, str, dict[str, Any]]:
    """POST to Ollama with `stream=true` and accumulate the streamed
    `message.content`. `idle_timeout` is a NO-PROGRESS (idle) timeout, not a total
    deadline: `urlopen`'s socket timeout applies to each read, so a model that keeps
    producing tokens never times out — only a truly-stalled one (crashed/deadlocked)
    does. This is what lets a merely-slow local model run as long as it needs.

    The third member is the FINAL streamed object — the one carrying `done: true`.
    Only that last chunk holds Ollama's `prompt_eval_count` / `eval_count`, so a
    caller wanting to account for the call has to be handed it here; by the time
    the text is joined the counts are gone."""
    _ai_throttle()
    data = json.dumps({**payload, "stream": True}).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    parts: list[str] = []
    final: dict[str, Any] = {}
    try:
        with urlopen(req, timeout=idle_timeout) as resp:
            if resp.status >= 400:
                return resp.status, resp.read().decode("utf-8", "replace"), final
            for raw_line in resp:  # each read may block up to idle_timeout
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message")
                if isinstance(message, dict) and message.get("content"):
                    parts.append(str(message["content"]))
                if obj.get("done"):
                    if isinstance(obj, dict):
                        final = obj
                    break
            return resp.status, "".join(parts), final
    except HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace"), final
    except OSError as err:
        raise ProviderCallError(f"NETWORK_ERROR: {err}") from err


def call_mistral_chat(
    prompt: str, model: str, timeout: int = 60, *, task: str = "", **api_params: Any
) -> str:
    # `task` is keyword-only and declared BEFORE **api_params on purpose: any name
    # landing in api_params is forwarded verbatim into the request payload, so a
    # plain keyword would be sent to Mistral as an unknown field.
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise ProviderCallError("MISTRAL_API_KEY is not set")

    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "model": model,
    }
    payload.update(_ai_sampling_params())  # env-configured sampling; neutral (unset) by default
    payload.update(api_params)  # an explicit call-site param still wins

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

    # Recorded only once the call is known to have succeeded. A rate-limited or
    # failed call is retried, and counting an attempt that produced nothing would
    # inflate the run's reported consumption above what is actually billed.
    record_response("mistral", model, task, body)
    return _extract_mistral_content(body)


def _ollama_num_ctx(default_value: int = 8192) -> int:
    """Context window for Ollama chat. Ollama defaults to only 2048 tokens, which
    silently TRUNCATES our long analysis prompts (a 6000-char document + rules) →
    empty/garbage output. Override with `PROCRAFILER_OLLAMA_NUM_CTX` to trade VRAM
    for context."""
    raw = os.environ.get("PROCRAFILER_OLLAMA_NUM_CTX", "")
    try:
        value = int(raw)
    except ValueError:
        return default_value
    return value if value > 0 else default_value


def call_ollama_chat(prompt: str, model: str, timeout: int = 60, *, task: str = "") -> str:
    # `format: "json"` constrains Ollama to emit valid JSON. Every text task that
    # uses this (analysis / organize / grouping) expects a JSON object, and small
    # local models otherwise wrap or mangle it. `num_ctx` lifts the 2048-token
    # default so the full prompt isn't truncated. Vision/OCR use a different
    # function (free text), so they are unaffected.
    #
    # STREAMING: the response is consumed token by token, and `timeout` is treated as
    # a NO-PROGRESS (idle) timeout — a slow local model that keeps producing is never
    # killed for being slow, only a truly-stalled one is. There is no total deadline.
    status_code, content, final = _post_ollama_chat_stream(
        OLLAMA_CHAT_URL,
        payload={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json",
            "options": {"num_ctx": _ollama_num_ctx()},
        },
        idle_timeout=timeout,
    )

    if status_code >= 400:
        raise ProviderCallError(f"OLLAMA_ERROR_{status_code}: {content}")
    if not content.strip():
        raise ProviderCallError("OLLAMA_BAD_RESPONSE: empty content")
    # Local, so free — but still recorded, because a run must be able to show that
    # its calls cost nothing rather than leave the user to assume it.
    record_response("ollama", model, task, final)
    return content.strip()


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
