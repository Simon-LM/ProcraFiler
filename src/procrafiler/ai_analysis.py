"""Unified AI analysis: read once, produce the whole document fiche.

Per spec §9, a single AI call consumes the already-read text and returns one
JSON object carrying everything the catalog stores about a document (§4.1): the
descriptive name, the document's own date, the destination category (+
alternatives), a summary, keywords, and structured entities. Naming and
classification are no longer separate passes — the catalog metadata "rides
along" in this one response, so making a document searchable costs no extra AI
call.

The provider/model is never hardcoded: the chain is read from
`PROCRAFILER_AI_ANALYSIS_PRIMARY` / `_FALLBACK`. With no chain configured, no
content, or after every provider fails, the result carries `used_fallback=True`
and empty metadata; the pipeline then names the file from its stem and routes it
to manual review. The AI never forces a destination (spec §7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
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
from procrafiler.naming import sanitize_filename_stem

# Cap the document text sent to the model: the gist is enough for naming,
# classification, and a summary. Keeps token cost and latency bounded.
MAX_CONTENT_CHARS = 6000


@dataclass(frozen=True)
class AnalysisResult:
    """Everything one analysis call produced about a document (its fiche).

    `name` / `category_path` are None when the AI gave none (the pipeline then
    falls back to the filename stem and/or manual review). `category_path` is
    the raw proposed path; the caller validates it against the taxonomy. All the
    other content fields are best-effort and may be empty.
    """

    name: str | None
    document_date: str | None
    category_path: str | None
    alternatives: list[str]
    summary: str | None
    keywords: list[str]
    entities: dict[str, Any]
    language: str | None
    provider: str
    model: str
    raw_output: str
    used_fallback: bool
    reason: str | None


def _empty_result(*, provider: str, model: str, reason: str, raw_output: str = "") -> AnalysisResult:
    return AnalysisResult(
        name=None,
        document_date=None,
        category_path=None,
        alternatives=[],
        summary=None,
        keywords=[],
        entities={},
        language=None,
        provider=provider,
        model=model,
        raw_output=raw_output,
        used_fallback=True,
        reason=reason,
    )


def _build_analysis_prompt(
    text: str,
    base_categories: list[str],
    existing_paths: list[str],
    original_filename: str | None = None,
    source_folder: str | None = None,
    user_context: str | None = None,
) -> str:
    bases = "\n".join(f"- {label}" for label in base_categories)
    tree = "\n".join(f"- {label}" for label in existing_paths) if existing_paths else "(none yet)"
    snippet = text[:MAX_CONTENT_CHARS]
    # Optional user context (passions, work, places, identity) to disambiguate —
    # e.g. tell a hobby from professional. Never authoritative over the content.
    context_block = ""
    if user_context:
        context_block = (
            "\nAbout the user (context to disambiguate — e.g. tell a hobby from professional, or "
            "anchor a person's identity; the document content still rules):\n"
            f"{user_context}\n"
            "When the document relates to the user's declared PROFESSION or business (a practice, "
            "training, tool, or event of that trade), prefer Work over Personal; the declared "
            "hobbies stay Personal.\n"
        )
    # The original filename and the folder the user dropped it in are HINTS, not
    # ground truth: the content stays authoritative, but these help when the
    # content is ambiguous (e.g. a file literally named "CV ...", or a photo in a
    # folder named "Water-Damage"). Generalist — no per-type rules.
    hints = ""
    if original_filename or source_folder:
        lines = ["\nHints (indicators, NOT ground truth — the content is authoritative; use these only to disambiguate):"]
        if original_filename:
            lines.append(f"- the user's original filename was: {original_filename}")
        if source_folder:
            lines.append(f"- it was dropped in a folder named: {source_folder}")
        hints = "\n".join(lines) + "\n"
    return (
        "Read this document and file it. Return JSON only, with this exact schema:\n"
        "{\"name\": \"...\", \"date\": \"YYYY-MM-DD\"|null, \"category_path\": \"...\"|null, "
        "\"alternatives\": [\"...\"], \"summary\": \"...\", \"keywords\": [\"...\"], "
        "\"entities\": {}, \"language\": \"fr\"}.\n\n"
        "Fields:\n"
        "- \"name\": a short, specific French title (no extension) identifying THIS exact document, "
        "named CONSISTENTLY so two documents of the SAME kind get the SAME structure. Lead with its "
        "most distinctive entity — the person, organization, or subject — then the key detail. Follow "
        "these patterns for common kinds, and keep the same spirit for others:\n"
        "    - CV/resume -> \"CV_<NOM>-<Prenom>\" — underscore after CV, family NAME in UPPERCASE "
        "(+ \"_<target role>\" if stated), e.g. CV_LOUVEL-Simon_Developpeur-web\n"
        "    - facture/bill -> \"Facture-<issuer>\" (+ object only if it adds information), e.g. Facture-EDF "
        "(NOT Facture-EDF-electricite: the issuer already implies it)\n"
        "    - bank statement -> \"Releve-<bank>\", e.g. Releve-BNP-Paribas\n"
        "    - attestation/certificat/diploma -> \"<Type>-<organisme>-<subject>\", e.g. "
        "Certificat-OpenClassrooms-Developpeur-web\n"
        "  Rules: do NOT put a DATE in the name (the filename already carries the date); do NOT add words "
        "already implied by the entity; do NOT name it by its file type or format; avoid empty words like "
        "\"document\", \"fichier\", \"texte\". When the user context gives the person's identity, use it "
        "(e.g. a CV -> that exact Nom-Prenom).\n"
        "- \"date\": the document's own date (letter/invoice/statement date) as YYYY-MM-DD if clearly "
        "stated in the content; otherwise null.\n"
        "- \"category_path\": MUST start with one of these existing base categories "
        "(you may NOT invent a new top-level category):\n"
        f"{bases}\n"
        "  Prefer an EXISTING folder from the current tree below if one fits; reuse its exact path. "
        "Only create a new subfolder (under a base) when none fits. If confident about the base but "
        "unsure about subfolders, return just the base. If you truly cannot tell, set it to null.\n"
        "  SERIES RULE: if the document is of an obviously RECURRING kind (meter reading, bank "
        "statement, payslip, bill, tax notice, insurance policy…), propose a series subfolder even "
        "for a single instance (e.g. \".../Housing/Releves-eau\") — and REUSE that exact folder "
        "path from the tree below when one already fits.\n"
        "- \"alternatives\": up to 3 other plausible category paths (each under a base). Always provide "
        "some when category_path is null or you are unsure.\n"
        "- \"summary\": 1-2 sentences in French on what the document is and its key point.\n"
        "- \"keywords\": 3-8 short lowercase French search terms.\n"
        "- \"entities\": a JSON object of key facts when present (e.g. issuer, doc_type, amounts, "
        "references, names); omit the ones you don't find.\n"
        "- \"language\": the document's main language code (e.g. \"fr\", \"en\").\n"
        "Do not add other keys or commentary.\n\n"
        "Current folder tree:\n"
        f"{tree}\n"
        f"{hints}"
        f"{context_block}\n"
        "Document content:\n"
        f"{snippet}"
    )


def _clean_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _clean_stem(value: Any) -> str | None:
    text = _clean_str(value)
    if text is None:
        return None
    return sanitize_filename_stem(text.strip("\"'"))


def _clean_path(value: Any) -> str | None:
    text = _clean_str(value)
    return text.strip("/") if text else None


def _clean_path_list(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            text = _clean_str(item)
            if text:
                out.append(text.strip("/"))
    return out


def _clean_keyword_list(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            text = _clean_str(item)
            if text:
                out.append(text)
    return out


def _extract_document_date(payload: dict[str, Any]) -> str | None:
    value = payload.get("date")
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return None
    return candidate


def analyze_content(
    text: str,
    *,
    base_categories: list[str],
    existing_paths: list[str],
    original_filename: str | None = None,
    source_folder: str | None = None,
    user_context: str | None = None,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> AnalysisResult:
    """Ask the configured AI for the full document fiche in one call.

    Returns an AnalysisResult. On no chain / no content / total failure it
    carries `used_fallback=True` and empty metadata; the caller then names the
    file from its stem and routes it to manual review. A valid JSON reply is a
    success even when individual fields (e.g. `category_path`) are null — the
    metadata is still captured, and routing is the caller's decision.
    """
    chain_entries = chain if chain is not None else task_chain_from_env("ANALYSIS")
    if not chain_entries:
        return _empty_result(provider="none", model="none", reason="chain_not_configured")
    if not text.strip():
        return _empty_result(provider="none", model="none", reason="no_content")

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("ANALYSIS", default_value=60)
    retry_count = retries if retries is not None else _task_retries_from_env("ANALYSIS", default_value=2)
    prompt = _build_analysis_prompt(
        text, base_categories, existing_paths, original_filename, source_folder, user_context
    )

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

                payload = _extract_json_dict(raw_output)
                if payload is None:
                    raise ProviderCallError("INVALID_JSON_RESPONSE")

                entities = payload.get("entities")
                return AnalysisResult(
                    name=_clean_stem(payload.get("name")),
                    document_date=_extract_document_date(payload),
                    category_path=_clean_path(payload.get("category_path")),
                    alternatives=_clean_path_list(payload.get("alternatives")),
                    summary=_clean_str(payload.get("summary")),
                    keywords=_clean_keyword_list(payload.get("keywords")),
                    entities=entities if isinstance(entities, dict) else {},
                    language=_clean_str(payload.get("language")),
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
