"""Set-aware organization: group a SET of already-analyzed documents into
dated affair / event / series folders.

Where `ai_analysis` decides ONE file at a time (and so can't see that several
files form a coherent set), this step looks at a whole set together — the files
dropped in one Inbox folder — and proposes a final placement for each, creating
shared, dated, named subfolders for the affair/event/series they belong to. It
is the "organize" phase; per spec it reads the fiches, not the files.

Two GENERALIST principles drive the prompt (never per-type hardcoded rules):
- group documents of the SAME affair/event/series into one dated, named folder;
- create a series folder even from a single instance when the document is of an
  obviously recurring kind (meter reading, statement, payslip, bill, tax notice…).

The provider/model is never hardcoded: the chain is read from
`PROCRAFILER_AI_ORGANIZE_PRIMARY` / `_FALLBACK` (a stronger model — e.g. Mistral
medium — is appropriate here). With no chain, or on failure, the result falls
back to each document's per-file proposed category (no grouping), so behavior
degrades to plain per-file classification. The caller validates every proposed
path against the taxonomy.
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

# Keep each document's summary short in the prompt: the set can be large and the
# organizer needs the gist, not the full text.
MAX_SUMMARY_CHARS = 200


@dataclass(frozen=True)
class OrganizeResult:
    # index (position in the input list) -> proposed final folder path (raw, to
    # be validated by the caller against the taxonomy), or None.
    placements: dict[int, str | None]
    provider: str
    model: str
    raw_output: str
    used_fallback: bool
    reason: str | None


def _fallback_placements(documents: list[dict[str, Any]]) -> dict[int, str | None]:
    """Each document keeps its own per-file proposed category — no grouping."""
    out: dict[int, str | None] = {}
    for index, document in enumerate(documents):
        proposed = document.get("category_path")
        out[index] = proposed if isinstance(proposed, str) and proposed.strip() else None
    return out


def _empty_result(*, provider: str, model: str, reason: str, documents: list[dict[str, Any]]) -> OrganizeResult:
    return OrganizeResult(
        placements=_fallback_placements(documents),
        provider=provider,
        model=model,
        raw_output="",
        used_fallback=True,
        reason=reason,
    )


def _build_organize_prompt(
    documents: list[dict[str, Any]],
    base_categories: list[str],
    existing_paths: list[str],
    source_folder: str | None,
    user_context: str | None = None,
) -> str:
    bases = "\n".join(f"- {label}" for label in base_categories)
    tree = "\n".join(f"- {label}" for label in existing_paths) if existing_paths else "(none yet)"

    lines: list[str] = []
    for index, document in enumerate(documents):
        name = str(document.get("name") or "?")
        date = str(document.get("document_date") or document.get("effective_date") or "?")
        proposed = str(document.get("category_path") or "?")
        origin = str(document.get("original_filename") or "")
        summary = str(document.get("summary") or "")[:MAX_SUMMARY_CHARS]
        origin_part = f" | original_filename: {origin}" if origin else ""
        lines.append(
            f"[{index}] name: {name} | date: {date} | proposed: {proposed}{origin_part} | summary: {summary}"
        )
    doc_block = "\n".join(lines)

    # The drop-folder is a STRONG HYPOTHESIS: files the user grouped on purpose
    # are, by DEFAULT, one coherent affair that must stay together in ONE folder.
    # Only a FLAGRANT misfit is split out (high bar). Vision descriptions of
    # photos may be hallucinated → never disperse the set on a shaky photo.
    if source_folder:
        hypothesis = (
            f"These {len(documents)} files were dropped together by the user in ONE folder named "
            f"\"{source_folder}\". Treat this as a STRONG HYPOTHESIS: by DEFAULT they ALL belong to the "
            "SAME affair/case and must go TOGETHER into ONE single destination folder. Your job is to "
            "find that one folder — NOT to sort them apart. Do not scatter a coherent set.\n\n"
        )
    else:
        hypothesis = ""

    # Optional user context (passions, work, places, identity) to disambiguate —
    # e.g. tell a hobby from professional. Never overrides the documents' content.
    if user_context:
        context_block = (
            "Context about the user — use what they DECLARED here as your reference when deciding "
            "where these documents belong (which subjects are personal vs professional in their "
            "life, who they are); the documents' content still rules:\n"
            f"{user_context}\n\n"
        )
    else:
        context_block = ""

    return (
        f"You are organizing a set of {len(documents)} documents that were already read.\n"
        f"{hypothesis}"
        f"{context_block}"
        "Decide a final folder for EACH document. Return JSON only, with this exact schema: "
        "{\"placements\": [{\"index\": 0, \"path\": \"...\"}, ...]} — one entry per document.\n\n"
        "Rules (in priority order):\n"
        "- \"path\" MUST start with one of these existing base categories "
        "(you may NOT invent a new top-level category):\n"
        f"{bases}\n"
        "- KEEP THE SET TOGETHER (default): put ALL the documents above into ONE shared folder — the "
        "SAME base category AND the SAME single subfolder — named for their common affair. Pick ONE "
        "base and ONE affair folder for the whole set; do NOT spread the same affair across two base "
        "categories (e.g. half in Housing, half in Insurance).\n"
        "- The DATE goes at the START of the folder name, NEVER at the end "
        "(e.g. \".../Insurance/2025-08_Degats-eaux-Annoville\"). Choose ONE period for the whole "
        "affair; do NOT create several date-variant folders (…2025-08 AND …2025-10) for the same affair.\n"
        "- Only a one-off AFFAIR/event folder is dated. A SERIES (recurring kind) is filed as "
        "<ENTITY>/<YEAR>: a folder named after its ENTITY (issuer/organism — EDF, Enercoop, "
        "BNP-Paribas — or the kind when there is no issuer — Releves-eau), NEVER dated, then a "
        "bare-YEAR subfolder (e.g. \".../Energy/EDF/2026\", \".../Housing/Releves-eau/2024\"). "
        "DIFFERENT entities are DIFFERENT series → DIFFERENT folders (an EDF bill and an Enercoop "
        "bill never share a folder).\n"
        "- Only place a document ELSEWHERE if its content is FLAGRANTLY a different case — a HIGH bar. "
        "A mere nuance is NOT a reason to split: a damage photo and the insurance form of the SAME "
        "affair belong in the SAME folder.\n"
        "- IMAGES were described by an AI vision model that CAN HALLUCINATE (wrong date, place, or "
        "subject) — their descriptions are NOT 100% reliable. NEVER split a file out of the set on the "
        "strength of a single shaky photo description; for an image-heavy set, trust the drop-folder "
        "grouping MORE than an individual photo's description.\n"
        "- Reuse an EXISTING folder from the tree below if one already fits this affair, rather than "
        "creating a near-duplicate.\n"
        "- Exception — a genuinely RECURRING kind on its own (meter reading, bank statement, payslip, "
        "bill, tax notice…) goes into an <ENTITY>/<YEAR> series subfolder, even from a single "
        "instance (e.g. \".../Energy/EDF/2026\", \".../Housing/Releves-eau/2024\").\n"
        "- Use short, normalized folder names (no accents needed). Do not add other keys or commentary.\n\n"
        "Current folder tree:\n"
        f"{tree}\n\n"
        "Documents:\n"
        f"{doc_block}"
    )


def _extract_placements(raw_output: str, documents: list[dict[str, Any]]) -> dict[int, str | None] | None:
    payload = _extract_json_dict(raw_output)
    if payload is None:
        return None
    raw_list = payload.get("placements")
    if not isinstance(raw_list, list):
        return None
    # Start from the per-file fallback so any document the model omits keeps its
    # own proposed category instead of being lost.
    placements = _fallback_placements(documents)
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        path = item.get("path")
        if not isinstance(index, int) or not (0 <= index < len(documents)):
            continue
        placements[index] = path.strip().strip("/") if isinstance(path, str) and path.strip() else None
    return placements


def organize_set(
    documents: list[dict[str, Any]],
    *,
    base_categories: list[str],
    existing_paths: list[str],
    source_folder: str | None = None,
    user_context: str | None = None,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> OrganizeResult:
    """Propose a final folder for each document in the set.

    Returns an OrganizeResult whose `placements` map each document's index to a
    proposed path (raw — the caller validates it against the taxonomy). With no
    chain configured or on total failure, each document falls back to its own
    per-file proposed category (no grouping).
    """
    if not documents:
        return OrganizeResult(placements={}, provider="none", model="none", raw_output="", used_fallback=True, reason="empty_set")

    chain_entries = chain if chain is not None else task_chain_from_env("ORGANIZE")
    if not chain_entries:
        return _empty_result(provider="none", model="none", reason="chain_not_configured", documents=documents)

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("ORGANIZE", default_value=90)
    retry_count = retries if retries is not None else _task_retries_from_env("ORGANIZE", default_value=2)
    prompt = _build_organize_prompt(documents, base_categories, existing_paths, source_folder, user_context)

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

                placements = _extract_placements(raw_output, documents)
                if placements is None:
                    raise ProviderCallError("INVALID_JSON_RESPONSE")
                return OrganizeResult(
                    placements=placements,
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

    return _empty_result(provider="fallback", model="fallback", reason=last_error, documents=documents)
