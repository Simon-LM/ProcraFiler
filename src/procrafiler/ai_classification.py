"""AI classification: decide a document's folder PATH from its CONTENT.

Per the IA-first principle (spec §9), the destination is decided by an AI
reading the file's content — never from the extension or the original filename.
This module takes already-read text (from `content_reader`) and asks the AI for
a folder path: it must start with one of the existing base categories, may reuse
or create subfolders under it, and must never invent a new top-level category.

Like `ai_naming`, the provider/model is never hardcoded: the chain is read from
`PROCRAFILER_AI_CLASSIFICATION_PRIMARY` / `_FALLBACK`. With no chain configured,
or if the AI fails or is uncertain, the result carries no path and the pipeline
routes the file to manual review. The AI never forces a destination (spec §7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
    # The AI's proposed folder path (e.g. "Administratif/Impots"), or None when
    # no chain is configured, every provider failed, or the AI was uncertain.
    # The path is validated/normalized by the caller (taxonomy), not here.
    path: str | None
    provider: str
    model: str
    raw_output: str
    used_fallback: bool
    reason: str | None
    # Candidate paths the AI considered — shown to the user by `review` when the
    # file lands in the decisions queue (uncertain / no confident path).
    alternatives: list[str] = field(default_factory=list)


def _build_classification_prompt(text: str, base_categories: list[str], existing_paths: list[str]) -> str:
    bases = "\n".join(f"- {label}" for label in base_categories)
    tree = "\n".join(f"- {label}" for label in existing_paths) if existing_paths else "(none yet)"
    snippet = text[:MAX_CONTENT_CHARS]
    return (
        "You file a document into a folder tree, based on its content.\n"
        "Return JSON only, with this exact schema: "
        "{\"path\": \"...\", \"alternatives\": [\"...\", \"...\"]}.\n\n"
        "Rules:\n"
        "- \"path\" MUST start with one of these existing base categories "
        "(you may NOT invent a new top-level category):\n"
        f"{bases}\n"
        "- Prefer an EXISTING folder from the current tree below if one fits; "
        "reuse its exact path instead of inventing a near-duplicate.\n"
        "- Only create a new subfolder (under a base category) when no existing "
        "folder fits. Use short, normalized names (no accents needed).\n"
        "- If you are confident about the base category but unsure about "
        "subfolders, return just the base category.\n"
        "- If you truly cannot tell, set \"path\" to null.\n"
        "- \"alternatives\": up to 3 other plausible paths (each also under a base "
        "category). Always provide some when you are unsure.\n"
        "- Do not add other keys or commentary.\n\n"
        "Current folder tree:\n"
        f"{tree}\n\n"
        "Document content:\n"
        f"{snippet}"
    )


def _extract_path_and_alternatives(raw_output: str) -> tuple[str | None, list[str]]:
    payload = _extract_json_dict(raw_output)
    if payload is None:
        return None, []
    raw_path = payload.get("path")
    path = raw_path.strip().strip("/") if isinstance(raw_path, str) and raw_path.strip() else None
    alternatives: list[str] = []
    raw_alts = payload.get("alternatives")
    if isinstance(raw_alts, list):
        for alt in raw_alts:
            if isinstance(alt, str) and alt.strip():
                alternatives.append(alt.strip().strip("/"))
    return path, alternatives


def classify_content(
    text: str,
    *,
    base_categories: list[str],
    existing_paths: list[str],
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> ClassificationResult:
    """Ask the configured AI for a folder path for `text`.

    Returns a ClassificationResult whose `path` is None when no chain is
    configured, when every provider fails, or when the AI is uncertain — in all
    those cases the caller routes the file to manual review. The path itself is
    validated against the taxonomy by the caller, not here.
    """
    chain_entries = chain if chain is not None else task_chain_from_env("CLASSIFICATION")
    if not chain_entries:
        return ClassificationResult(
            path=None, provider="none", model="none", raw_output="", used_fallback=True, reason="chain_not_configured"
        )

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("CLASSIFICATION", default_value=60)
    retry_count = retries if retries is not None else _task_retries_from_env("CLASSIFICATION", default_value=2)
    prompt = _build_classification_prompt(text, base_categories, existing_paths)

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

                path, alternatives = _extract_path_and_alternatives(raw_output)
                if path is not None:
                    return ClassificationResult(
                        path=path,
                        provider=entry.provider,
                        model=entry.model,
                        raw_output=raw_output,
                        used_fallback=False,
                        reason=None,
                        alternatives=alternatives,
                    )
                if alternatives:
                    # The AI deliberately declined to pick (null path) but offered
                    # options → not an error, route to the decisions queue.
                    return ClassificationResult(
                        path=None,
                        provider=entry.provider,
                        model=entry.model,
                        raw_output=raw_output,
                        used_fallback=True,
                        reason="uncertain_with_options",
                        alternatives=alternatives,
                    )
                # No path and no options (or unparseable). Retry / fail over.
                raise ProviderCallError("UNCLASSIFIED_OR_UNCERTAIN")
            except (RateLimitedError, ProviderCallError) as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    sleep_fn(2**attempt)
                    continue
                break

    return ClassificationResult(
        path=None, provider="fallback", model="fallback", raw_output="", used_fallback=True, reason=last_error
    )
