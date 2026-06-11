"""Lightweight grouping step for singleton files.

After a root singleton's per-file analysis has produced a confirmed route,
this step asks the AI to compare the new document against the *names* of
existing files along the candidate destination branches — and to decide
whether the new file and some of those existing files should share a common
series or affair folder.

Scope: root singletons only. Folder-sets are handled by `ai_organize`.

The chain is deliberately read from the ANALYSIS task
(`PROCRAFILER_AI_ANALYSIS_*`) — no new env var needed — since this is a
lightweight "confirm or regroup" step whose cost is comparable to a
per-file analysis call.
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

# Cap total listing characters across all branches to keep the prompt bounded.
MAX_LISTING_CHARS = 2500


@dataclass(frozen=True)
class GroupingResult:
    """Outcome of a grouping proposal for one singleton document.

    `path` is the confirmed or proposed common destination (raw, caller
    validates against the taxonomy). `group_with` names existing files that
    should be moved to that destination too (empty = new file only, no
    existing file to regroup). `used_fallback` is True when no AI call was
    made or the call failed — `path` is then None and `group_with` is empty.
    """

    path: str | None
    group_with: list[str]
    provider: str
    model: str
    raw_output: str
    used_fallback: bool
    reason: str | None


def _empty_grouping_result(*, provider: str, model: str, reason: str) -> GroupingResult:
    return GroupingResult(
        path=None,
        group_with=[],
        provider=provider,
        model=model,
        raw_output="",
        used_fallback=True,
        reason=reason,
    )


def _build_grouping_prompt(
    document: dict[str, Any],
    candidate_branches: dict[str, list[str]],
) -> str:
    name = str(document.get("name") or "?")
    summary = str(document.get("summary") or "")[:200]
    original = str(document.get("original_filename") or "")
    origin_part = f" | original: {original}" if original else ""

    branches_parts: list[str] = []
    total_chars = 0
    for branch_path, filenames in candidate_branches.items():
        if total_chars >= MAX_LISTING_CHARS:
            break
        if filenames:
            names_str = ", ".join(filenames)
            budget = MAX_LISTING_CHARS - total_chars - len(branch_path) - 10
            if len(names_str) > budget and budget > 20:
                names_str = names_str[:budget] + "…"
            elif budget <= 20:
                names_str = "(truncated)"
            total_chars += len(names_str) + len(branch_path) + 10
            branches_parts.append(f"  {branch_path}:\n    {names_str}")
        else:
            branches_parts.append(f"  {branch_path}: (empty)")
    branches_block = "\n".join(branches_parts)

    return (
        "You are filing ONE new document. Confirm its destination OR propose a common SERIES "
        "folder if the new document clearly belongs to the same recurring series as existing files. "
        "Return JSON only: {\"path\": \"...\"|null, \"group_with\": [\"...\"]}\n\n"
        "New document:\n"
        f"  name: {name}{origin_part} | summary: {summary}\n\n"
        "Candidate destination branches (folder path → existing filenames inside):\n"
        f"{branches_block}\n\n"
        "Rules:\n"
        "- Confirm ONE path from the candidates, OR propose a NEW SERIES SUBFOLDER under one of "
        "them when the new document is manifestly of the same recurring kind as existing files — "
        "and list those files' names in \"group_with\".\n"
        "- The DATE goes at the START of any new folder name (e.g. \".../Housing/2025_Releves-eau\"), "
        "NEVER at the end.\n"
        "- HIGH BAR: only regroup when the similarity is OBVIOUS (same recurring series or same "
        "affair). When in doubt, confirm the original path and leave \"group_with\" empty.\n"
        "- Existing filenames were produced by an AI — treat them as CLUES, not absolute truth.\n"
        "- \"group_with\" must contain EXACT filenames from the listing above; never invent names.\n"
        "- \"path\" null means: keep the per-file analysis route unchanged.\n"
        "- Do not add other keys or commentary."
    )


def _extract_grouping_result(raw_output: str, *, provider: str, model: str) -> GroupingResult | None:
    payload = _extract_json_dict(raw_output)
    if payload is None:
        return None
    path_raw = payload.get("path")
    path: str | None = path_raw.strip().strip("/") if isinstance(path_raw, str) and path_raw.strip() else None
    group_with: list[str] = []
    group_with_raw = payload.get("group_with")
    if isinstance(group_with_raw, list):
        for item in group_with_raw:
            if isinstance(item, str) and item.strip():
                group_with.append(item.strip())
    return GroupingResult(
        path=path,
        group_with=group_with,
        provider=provider,
        model=model,
        raw_output=raw_output,
        used_fallback=False,
        reason=None,
    )


def propose_grouping(
    document: dict[str, Any],
    candidate_branches: dict[str, list[str]],
    *,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> GroupingResult:
    """Propose a grouping decision for ONE singleton document.

    `document` carries `name`, `summary`, `original_filename` (all optional).
    `candidate_branches` maps each candidate folder path (string) to the list
    of existing filenames inside that folder (see `_list_branch_files` in
    pipeline.py for how this dict is built).

    Returns early (no AI call) when all branches are empty or when no ANALYSIS
    chain is configured. On failure falls back gracefully to a no-op result.
    """
    if not candidate_branches or all(not v for v in candidate_branches.values()):
        return _empty_grouping_result(provider="none", model="none", reason="no_candidate_files")

    chain_entries = chain if chain is not None else task_chain_from_env("ANALYSIS")
    if not chain_entries:
        return _empty_grouping_result(provider="none", model="none", reason="chain_not_configured")

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("ANALYSIS", default_value=60)
    retry_count = retries if retries is not None else _task_retries_from_env("ANALYSIS", default_value=2)
    prompt = _build_grouping_prompt(document, candidate_branches)

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

                result = _extract_grouping_result(raw_output, provider=entry.provider, model=entry.model)
                if result is None:
                    raise ProviderCallError("INVALID_JSON_RESPONSE")
                return result
            except (RateLimitedError, ProviderCallError) as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    sleep_fn(2**attempt)
                    continue
                break

    return _empty_grouping_result(provider="fallback", model="fallback", reason=last_error)
