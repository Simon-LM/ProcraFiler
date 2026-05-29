"""AI classification: decide a document's category from its CONTENT.

Per the IA-first principle (spec §9), the destination category is decided by
an AI reading the file's content — never from the extension or the original
filename. This module takes already-read text (from `content_reader`) and asks
the configured AI to pick one category among the taxonomy.

Like `ai_naming`, the provider/model is never hardcoded: the chain is read from
`PROCRAFILER_AI_CLASSIFICATION_PRIMARY` / `_FALLBACK`. With no chain configured,
or if the AI fails or is uncertain, classification falls back to "no category"
and the pipeline routes the file to manual review. The AI never forces a
destination — uncertain outcomes always go to a human (spec §7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
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

# Cap the document text sent to the model. Classification needs the gist, not
# the whole file; this keeps token cost and latency bounded on large documents.
MAX_CONTENT_CHARS = 6000


@dataclass(frozen=True)
class ClassificationResult:
    category: str | None
    provider: str
    model: str
    raw_output: str
    used_fallback: bool
    reason: str | None


def _build_classification_prompt(text: str, allowed_categories: list[str]) -> str:
    options = "\n".join(f"- {label}" for label in allowed_categories)
    snippet = text[:MAX_CONTENT_CHARS]
    return (
        "You classify a document into exactly one category, based on its content.\n"
        "Return JSON only, with this exact schema: {\"category\": \"...\"}.\n"
        "The value must be EXACTLY one of the allowed categories listed below, "
        "copied verbatim. If you are not confident, return {\"category\": null}.\n"
        "Do not invent categories. Do not add other keys or commentary.\n\n"
        "Allowed categories:\n"
        f"{options}\n\n"
        "Document content:\n"
        f"{snippet}"
    )


def _extract_category(raw_output: str, allowed_categories: list[str]) -> str | None:
    payload = _extract_json_dict(raw_output)
    if payload is None:
        return None
    value = payload.get("category")
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    # Exact match only — the model was told to copy a category verbatim. This
    # prevents a hallucinated or near-miss label from sending a file to a wrong
    # (or non-existent) folder.
    return candidate if candidate in allowed_categories else None


def classify_content(
    text: str,
    *,
    allowed_categories: list[str],
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> ClassificationResult:
    """Ask the configured AI to classify `text` into one allowed category.

    Returns a ClassificationResult whose `category` is None when no chain is
    configured, when every provider fails, or when the AI is uncertain — in all
    those cases the caller must route the file to manual review.
    """
    chain_entries = chain if chain is not None else task_chain_from_env("CLASSIFICATION")
    if not chain_entries:
        return ClassificationResult(
            category=None,
            provider="none",
            model="none",
            raw_output="",
            used_fallback=True,
            reason="chain_not_configured",
        )

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("CLASSIFICATION", default_value=60)
    retry_count = retries if retries is not None else _task_retries_from_env("CLASSIFICATION", default_value=2)
    prompt = _build_classification_prompt(text, allowed_categories)

    last_error = "unknown"
    for entry in chain_entries:
        for attempt in range(retry_count + 1):
            try:
                if entry.provider == "mistral":
                    raw_output = call_mistral_chat(prompt, entry.model, timeout=timeout, temperature=0.0)
                elif entry.provider == "ollama":
                    raw_output = call_ollama_chat(prompt, entry.model, timeout=timeout)
                else:
                    raise ProviderCallError(f"unsupported_provider:{entry.provider}")

                category = _extract_category(raw_output, allowed_categories)
                if category is None:
                    # Either invalid JSON or an explicit/!allowed value. Treat as
                    # a provider miss so we retry / fail over; if it persists the
                    # file ends up in manual review.
                    raise ProviderCallError("UNCLASSIFIED_OR_UNCERTAIN")
                return ClassificationResult(
                    category=category,
                    provider=entry.provider,
                    model=entry.model,
                    raw_output=raw_output,
                    used_fallback=False,
                    reason=None,
                )
            except (RateLimitedError, ProviderCallError) as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    sleep_fn(2**attempt)
                    continue
                break

    return ClassificationResult(
        category=None,
        provider="fallback",
        model="fallback",
        raw_output="",
        used_fallback=True,
        reason=last_error,
    )
