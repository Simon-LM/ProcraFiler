"""Grouping step for singleton files: confirm the destination, or creuse.

After a root singleton's per-file analysis has produced a confirmed route,
this step shows the AI the existing files along the candidate destination
branches (paths relative to each branch) and asks one question: is this new
document part of a series/affair that deserves a DEEPER shared subfolder —
and if so, which already-filed files should move down into it?

Scope: root singletons only. Folder-sets are handled by `ai_organize`.

The run invariant (spec §1.2) bounds everything this step can cause: the
pipeline only honors a proposed path that is a STRICT descendant of a
candidate branch, and only moves existing files STRICTLY DEEPER than where
they sit. The prompt describes that contract; the locks enforce it.

The chain is read from the ORGANIZE task (`PROCRAFILER_AI_ORGANIZE_*`, e.g.
Mistral medium): deciding that already-filed documents belong together takes
the same judgment as organizing a set — the third real run showed a small
model inverting the grouping semantics.
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
    existing file to regroup). `name` is an optional consistent stem for the
    NEW file when it joins a populated series (so siblings match); None to keep
    the analysis name. `used_fallback` is True when no AI call was made or the
    call failed — `path` is then None and `group_with`/`name` are empty.
    """

    path: str | None
    group_with: list[str]
    name: str | None
    provider: str
    model: str
    raw_output: str
    used_fallback: bool
    reason: str | None


def _empty_grouping_result(*, provider: str, model: str, reason: str) -> GroupingResult:
    return GroupingResult(
        path=None,
        group_with=[],
        name=None,
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
        "You are filing ONE new document into a library. Decide whether it reveals a SERIES or "
        "AFFAIR that deserves a shared subfolder. "
        "Return JSON only: {\"path\": \"...\"|null, \"group_with\": [\"...\"], \"name\": \"...\"|null}\n\n"
        "New document:\n"
        f"  name: {name}{origin_part} | summary: {summary}\n\n"
        "Candidate destination branches (branch folder → existing files inside, as paths relative "
        "to that branch):\n"
        f"{branches_block}\n\n"
        "Rules:\n"
        "- Either CONFIRM one of the candidate branch paths as-is, OR propose ONE shared "
        "series/affair SUBFOLDER strictly DEEPER under one of them (e.g. branch + \"/Releves-eau\"). "
        "NEVER propose a parent folder, a sibling branch, or any path outside the candidates: files "
        "may only ever move DOWN into a more specific folder, never up, never sideways.\n"
        "- \"group_with\": ONLY when you proposed a deeper shared subfolder — list the existing "
        "files (their relative paths, copied EXACTLY from the listing) that MANIFESTLY belong to "
        "that same series/affair AND currently sit in an ANCESTOR folder of it. A file already "
        "inside a well-named subfolder (e.g. a dated affair folder) is already organized — leave it "
        "alone, do NOT list it.\n"
        "- HIGH BAR: only group what is OBVIOUS (same recurring series, same affair). When in "
        "doubt, confirm the original path and leave \"group_with\" empty.\n"
        "- A SERIES subfolder is named after its ENTITY (issuer/organism — EDF, Enercoop, "
        "BNP-Paribas — or the kind when there is no issuer — Releves-eau) and is NEVER dated; the "
        "period is a bare-YEAR subfolder INSIDE it (e.g. \"Energy/EDF/2026\", \"Releves-eau/2026\"). "
        "Only a one-off AFFAIR folder is dated, its DATE at the START (e.g. "
        "\".../Housing/2025-08_Degats-eaux\"), NEVER at the end.\n"
        "- DIFFERENT entities are DIFFERENT series: NEVER group documents from different issuers "
        "together (an EDF bill and an Enercoop bill do NOT share a folder, even though both are "
        "electricity bills). Only group an existing file that shares the SAME entity AND the SAME "
        "year as the new document.\n"
        "- \"name\": ONLY when you place the new document into a series subfolder that ALREADY "
        "contains files of the SAME kind — rewrite the new document's name to follow the SAME "
        "structure as those existing files (same components, same order, e.g. all \"Releve_eau\"), "
        "so siblings are named consistently. Give just the descriptive stem (no date, no extension). "
        "Otherwise set it to null.\n"
        "- Existing file names were produced by an AI — treat them as CLUES, not absolute truth.\n"
        "- \"path\" null means: keep the originally proposed destination unchanged.\n"
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
    name_raw = payload.get("name")
    name: str | None = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else None
    return GroupingResult(
        path=path,
        group_with=group_with,
        name=name,
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
    `candidate_branches` maps each candidate branch label to the existing files
    inside it, as branch-relative paths (see `_list_branch_files` in
    pipeline.py for how this dict is built).

    Returns early (no AI call) when all branches are empty or when no ORGANIZE
    chain is configured. On failure falls back gracefully to a no-op result.
    """
    if not candidate_branches or all(not v for v in candidate_branches.values()):
        return _empty_grouping_result(provider="none", model="none", reason="no_candidate_files")

    chain_entries = chain if chain is not None else task_chain_from_env("ORGANIZE")
    if not chain_entries:
        return _empty_grouping_result(provider="none", model="none", reason="chain_not_configured")

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("ORGANIZE", default_value=90)
    retry_count = retries if retries is not None else _task_retries_from_env("ORGANIZE", default_value=2)
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
