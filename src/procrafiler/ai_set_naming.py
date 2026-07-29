"""Set-aware NAMING: decide the final name of every file of a dropped folder, at
once, in the light of the others.

Where `ai_analysis` names ONE file from its own content — blind to everything
around it — this pass runs AFTER the whole set has been analysed and re-judges the
names together. It exists because a name derived from a single file's content can
simply be wrong, and the surrounding files are the evidence that reveals it. The
worst case is a photo: a vision model describing a scene with no legible text can
produce a confident, entirely wrong description, while the nine files dropped
alongside it say plainly what the set is about.

It is NOT a grouping mechanism. Taking the context into account tends to make
coherent groupings emerge, but that is a consequence, never an instruction: a
dropped folder may legitimately hold several unrelated subjects (different
administrative senders, say), and then each file keeps its own identity.

The reasoning lives in the PROMPT, not in this module: no scoring, no per-case
rules, no deterministic outlier detection. Semantic coherence — seeing that EDF,
CAF and AMELI belong to one theme though they share no words — is exactly what a
model does and code cannot. The code's job is to hand over the full context and to
guard the file operations afterwards.

Provider/model from `PROCRAFILER_AI_NAMING_PRIMARY` / `_FALLBACK` (a capable model
is appropriate: the pass sees the whole set at once). With no chain, or on
failure, every file keeps the name its own analysis proposed — behaviour degrades
to exactly what it was before this pass existed.
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

# The set can be large; the pass needs each document's gist, not its full text.
MAX_SUMMARY_CHARS = 200


@dataclass(frozen=True)
class NamedFile:
    """The pass's verdict for one file. `name` is raw — the caller sanitizes it."""

    name: str | None
    needs_review: bool
    reason: str | None


@dataclass(frozen=True)
class SetNamingResult:
    # index (position in the input list) -> verdict. A missing index means the
    # model said nothing about that file: it keeps its analysis name.
    names: dict[int, NamedFile]
    provider: str
    model: str
    raw_output: str
    used_fallback: bool
    reason: str | None


def _empty_result(*, provider: str, model: str, reason: str | None) -> SetNamingResult:
    """No chain, or every provider failed: every file keeps its analysis name."""
    return SetNamingResult(
        names={}, provider=provider, model=model, raw_output="", used_fallback=True, reason=reason
    )


def _describe(documents: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, document in enumerate(documents):
        read_via = str(document.get("read_via") or "unknown")
        original = str(document.get("original_filename") or "?")
        proposed = str(document.get("proposed_name") or "?")
        summary = str(document.get("summary") or "")[:MAX_SUMMARY_CHARS]
        lines.append(
            f"[{index}] read_as: {read_via} | original_filename: {original} | "
            f"proposed_name: {proposed} | summary: {summary}"
        )
    return "\n".join(lines)


def _build_set_naming_prompt(
    documents: list[dict[str, Any]],
    source_folder: str | None,
    user_context: str | None,
) -> str:
    folder = source_folder or "(unnamed)"
    context_block = (
        f"\nAbout the user (use to disambiguate — who they are, what is their work vs their "
        f"hobbies):\n{user_context}\n"
        if user_context
        else ""
    )
    return (
        f"These files were dropped TOGETHER by the user in one folder named \"{folder}\". "
        "Each was read and analysed ALONE; you now see them all at once. Decide each file's "
        "final name in the light of the others.\n"
        f"{context_block}"
        "\nThe folder name, the original filenames and the names each analysis proposed are "
        "STRONG CLUES, never certainties. Content read mechanically (text file, PDF text layer) "
        "is literal, and OCR transcription of a document is reliable too — text read off a page "
        "is still text. The weak source is IMAGE DESCRIPTION: a vision model interpreting a "
        "photo, most of all a photo with no legible text to anchor it, where a confident "
        "description can be entirely wrong.\n"
        "\nSo when one file's reading stands out as strongly incoherent with the folder and "
        "everything in it, the likeliest explanation is a MISREAD photo, not a stranger among "
        "them: trust the context and name that file as part of what surrounds it.\n"
        "\nBut weigh what the reading gets RIGHT, too. A vision model confuses materials and "
        "textures — a soaked carpet for a lawn, crumpled bodywork for a sculpture — it does not "
        "invent daylight, a blue sky, an outdoor setting, or a plainly dry and tidy room. When "
        "such SCENE facts put a file outside the set beyond doubt, it IS a stranger: leave its "
        "name alone. Recontextualise a doubtful TEXTURE, never a clear SETTING.\n"
        "\nBut a setting only excludes when the SUBJECT is unrelated too. If the reading shows the "
        "very phenomenon the set is about — even in an unexpected place — the file may well belong "
        "to it: it can be the cause, the extent, or another instance of the same event. A sunny "
        "lawn has nothing to do with a water-damage claim; a FLOODED one may be its source. Exclude "
        "on setting only when nothing in the subject connects.\n"
        "\nDo not force one subject onto the folder — it may legitimately hold several "
        "(different senders, different affairs), and then each file keeps its own identity. "
        "Two documents of the same kind get the same name structure.\n"
        "\nAn original filename is a strong clue to WHAT a document is, but rarely a good name: "
        "it may be hurried, partial, or plainly wrong. Improve on it when the content confirms "
        "it; leave an already-correct name alone.\n"
        "\nWhen a reading MIXES both — a setting that puts the file outside the set AND a sign of "
        "the very phenomenon the set is about — and nothing else settles which of the two is the "
        "real subject, do not choose: set review to true. That combination is exactly where a "
        "vision reading is least trustworthy, and picking either way silently is worse than "
        "asking.\n"
        "\nOtherwise JUDGE FREELY: name the file yourself rather than asking, whenever one reading "
        "clearly prevails.\n"
        "\nThe files:\n"
        f"{_describe(documents)}\n"
        "\nReturn JSON only: {\"files\": [{\"index\": 0, \"name\": \"...\", \"review\": true|false, "
        "\"why\": \"one short line\"}, ...]}. Give the name WITHOUT extension and WITHOUT any "
        "date prefix (the system adds the date itself). One entry per file.\n"
    )


def _extract_names(raw_output: str, documents: list[dict[str, Any]]) -> dict[int, NamedFile] | None:
    payload = _extract_json_dict(raw_output)
    if payload is None:
        return None
    items = payload.get("files")
    if not isinstance(items, list):
        return None

    names: dict[int, NamedFile] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or not (0 <= index < len(documents)):
            continue
        raw_name = item.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        why = item.get("why")
        names[index] = NamedFile(
            name=name,
            needs_review=bool(item.get("review")),
            reason=why.strip() if isinstance(why, str) and why.strip() else None,
        )
    return names


def name_set(
    documents: list[dict[str, Any]],
    *,
    source_folder: str | None = None,
    user_context: str | None = None,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> SetNamingResult:
    """Re-judge the names of a whole dropped set, together.

    Each document is a dict with `read_via`, `original_filename`, `proposed_name`
    and `summary`. Returns a verdict per index; any index the model omits keeps
    its analysis name. With no chain or on total failure, nothing is renamed.
    """
    if not documents:
        return _empty_result(provider="none", model="none", reason="empty_set")

    chain_entries = chain if chain is not None else task_chain_from_env("NAMING")
    if not chain_entries:
        return _empty_result(provider="none", model="none", reason="chain_not_configured")

    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _task_timeout_from_env("NAMING", default_value=90, provider=chain_entries[0].provider)
    )
    retry_count = retries if retries is not None else _task_retries_from_env("NAMING", default_value=2)
    prompt = _build_set_naming_prompt(documents, source_folder, user_context)

    last_error = "unknown"
    for entry in chain_entries:
        for attempt in range(retry_count + 1):
            try:
                if entry.provider == "mistral":
                    raw_output = call_mistral_chat(prompt, entry.model, timeout=timeout)
                elif entry.provider == "ollama":
                    raw_output = call_ollama_chat(prompt, entry.model, timeout=timeout)
                else:
                    raise ProviderCallError(f"unsupported_provider:{entry.provider}")

                names = _extract_names(raw_output, documents)
                if names is None:
                    raise ProviderCallError("INVALID_JSON_RESPONSE")
                return SetNamingResult(
                    names=names,
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

    return _empty_result(provider="fallback", model="fallback", reason=last_error)
