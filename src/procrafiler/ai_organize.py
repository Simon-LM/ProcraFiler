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
    documents: list[dict[str, Any]], base_categories: list[str], existing_paths: list[str], source_folder: str | None
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

    # The drop-folder is a STRONG HYPOTHESIS, not a certainty: the user grouped
    # these files on purpose, so they PROBABLY form one coherent set and the
    # folder name is PROBABLY the right theme. Base the grouping on it — but the
    # CONTENT is authoritative and can override it.
    if source_folder:
        hypothesis = (
            f"These files were dropped together by the user in a folder named \"{source_folder}\". "
            "Treat that as a STRONG HYPOTHESIS, not a certainty: assume they PROBABLY belong to the "
            "same affair/case and that the folder name is PROBABLY the right theme — make this your "
            "starting point and lean toward keeping them together under one folder named for it. "
            "BUT verify against each document's own content: if a file clearly does not belong, place "
            "it where ITS content says (split it out of the set); if the content contradicts the "
            "folder name, the content wins. Privilégier, pas imposer.\n\n"
        )
    else:
        hypothesis = ""

    return (
        f"You are organizing a set of {len(documents)} documents that were already read.\n"
        f"{hypothesis}"
        "Decide a final folder for EACH document. Return JSON only, with this exact schema: "
        "{\"placements\": [{\"index\": 0, \"path\": \"...\"}, ...]} — one entry per document.\n\n"
        "Rules:\n"
        "- \"path\" MUST start with one of these existing base categories "
        "(you may NOT invent a new top-level category):\n"
        f"{bases}\n"
        "- Group documents that belong to the SAME affair, event, or case into ONE shared subfolder, "
        "named for that affair and its period (e.g. \".../Insurance/Degats-eaux-2025-07\"). "
        "Use the documents' dates, their content, and the drop-folder hypothesis above as signals.\n"
        "- For a document of an obviously RECURRING kind (meter reading, bank statement, payslip, "
        "bill, tax notice…), put it in a series subfolder, created even from a single instance "
        "(e.g. \".../Housing/Releves-eau\").\n"
        "- Do NOT force unrelated documents together; a genuine one-off may stay directly in its base "
        "category. Prefer reusing an EXISTING folder from the tree below over creating a near-duplicate.\n"
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
    prompt = _build_organize_prompt(documents, base_categories, existing_paths, source_folder)

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
