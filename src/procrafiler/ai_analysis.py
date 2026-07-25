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
    series: bool
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
        series=False,
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


_LANG_NAMES = {
    "fr": "French", "en": "English", "es": "Spanish",
    "de": "German", "it": "Italian", "pt": "Portuguese",
}


def _summary_and_keyword_instructions(user_language: str) -> tuple[str, str]:
    """How to phrase the summary and keywords for the user's language. Keywords are
    asked in English AND the user's language so search works either way; the
    summary is in the user's language. Falls back to English-only."""
    code = (user_language or "en").lower()
    name = _LANG_NAMES.get(code, code)
    if code == "en":
        return ("1-2 sentences in English", "3-8 short lowercase English search terms")
    return (
        f"1-2 sentences in {name}",
        f"6-12 short lowercase search terms covering the document's salient words in BOTH English and {name}",
    )


# How many sibling filenames to show, and how long the list may get. Files dropped
# together are a set, but a 200-file folder must not swamp the prompt (or the bill).
MAX_SIBLING_HINTS = 12
MAX_SIBLING_CHARS = 400

# Content read by an AI (a vision model describing an image, OCR on a scan) is
# itself an interpretation and can be wrong or hallucinated. Content extracted
# mechanically (a PDF text layer, a .txt file) is literal bytes.
_INTERPRETED_READS = ("vision", "ocr")


def _build_hints_block(
    *,
    original_filename: str | None,
    source_folder: str | None,
    sibling_filenames: list[str] | None = None,
    read_via: str | None = None,
) -> str:
    """The hints block: the original filename, the drop folder, and the names of the
    files dropped alongside.

    "Never trust the filename" means the name must never *decide* — NOT that it is
    discarded. It stays a strong indicator, and it must weigh MORE the less reliable
    the extracted content is. So the framing depends on `read_via`:

    - mechanical read (`text`): the content is literal bytes → authoritative, hints
      only disambiguate.
    - AI read (`vision` / `ocr`): the "content" is itself a model's interpretation of
      an image and can be wrong or hallucinated. Here the filename and the sibling
      names are CORROBORATING EVIDENCE, and a confident name that clearly contradicts
      a vague visual description should win — or go to the decisions queue rather
      than be guessed. Telling the model the content is authoritative in this case
      would be exactly backwards.

    Sibling names matter most in precisely that case: a photo among clearly-named
    documents inherits their context. The organize pass cannot substitute for this —
    it works on fiches already produced, so a misreading has already happened.
    """
    interpreted = read_via in _INTERPRETED_READS
    if not (original_filename or source_folder or sibling_filenames):
        return ""

    if interpreted:
        header = (
            "\nCorroborating evidence — IMPORTANT: the text above was produced by an AI "
            "reading an image (OCR/vision), so it may be incomplete, misread or invented. "
            "These facts come from the user's own filesystem and are RELIABLE. Weigh them "
            "heavily: when the visual description is vague or generic but the evidence below "
            "is specific, FOLLOW THE EVIDENCE. If the two clearly contradict each other, "
            "prefer the evidence, or return category_path null with alternatives rather than "
            "guessing from the image alone:"
        )
    else:
        header = (
            "\nHints (indicators, NOT ground truth — the content is authoritative; use these "
            "only to disambiguate):"
        )

    lines = [header]
    if original_filename:
        lines.append(f"- the user's original filename was: {original_filename}")
    if source_folder:
        lines.append(f"- it was dropped in a folder named: {source_folder}")
    if sibling_filenames:
        shown: list[str] = []
        budget = MAX_SIBLING_CHARS
        for name in sibling_filenames[:MAX_SIBLING_HINTS]:
            if budget - len(name) < 0:
                break
            shown.append(name)
            budget -= len(name)
        if shown:
            lines.append(
                "- it was dropped together with these files (same set — a strong clue about "
                f"what this document is about): {', '.join(shown)}"
            )
    return "\n".join(lines) + "\n"


def _build_analysis_prompt(
    text: str,
    base_categories: list[str],
    existing_paths: list[str],
    original_filename: str | None = None,
    source_folder: str | None = None,
    user_context: str | None = None,
    user_language: str = "en",
    sibling_filenames: list[str] | None = None,
    read_via: str | None = None,
) -> str:
    bases = "\n".join(f"- {label}" for label in base_categories)
    tree = "\n".join(f"- {label}" for label in existing_paths) if existing_paths else "(none yet)"
    snippet = text[:MAX_CONTENT_CHARS]
    summary_instruction, keywords_instruction = _summary_and_keyword_instructions(user_language)
    name_language = _LANG_NAMES.get((user_language or "en").lower(), user_language or "en")
    # Optional user context (passions, work, places, identity) to disambiguate —
    # e.g. tell a hobby from professional. Never authoritative over the content.
    context_block = ""
    if user_context:
        context_block = (
            "\nAbout the user — use the facts DECLARED here to disambiguate (which subjects are the "
            "user's hobbies vs their job, who the person is); the document's content still decides "
            "WHAT it is:\n"
            f"{user_context}\n"
            "For the Personal-vs-Work axis, go by the user's RELATIONSHIP to the document as declared "
            "above, not by how professional the content looks: if it concerns one of the user's stated "
            "HOBBIES it is Personal — even when the equipment, skill, or venue is professional-grade; "
            "only a document of the user's stated JOB or business leans Work.\n"
            "If the context lists names (employer, business, clients, projects, tools) that mean the "
            "user's WORK, a document about any of them leans Work too — but the user's work is NOT "
            "limited to that list: judge clearly professional content as Work even when its name is "
            "not listed. Apply this the SAME WAY every time: a document mentioning a declared "
            "work-name belongs under Work even when an existing Personal/Hobbies folder looks related "
            "— never let an existing hobby folder pull a work document into it.\n"
        )
    hints = _build_hints_block(
        original_filename=original_filename,
        source_folder=source_folder,
        sibling_filenames=sibling_filenames,
        read_via=read_via,
    )
    return (
        "Read this document and file it. Return JSON only, with this exact schema:\n"
        "{\"name\": \"...\", \"date\": \"YYYY-MM-DD\"|null, \"category_path\": \"...\"|null, "
        "\"series\": true|false, \"alternatives\": [\"...\"], \"summary\": \"...\", "
        "\"keywords\": [\"...\"], \"entities\": {}, \"language\": \"fr\"}.\n\n"
        "Fields:\n"
        f"- \"name\": a short, specific {name_language} title (no extension) identifying THIS exact document, "
        "named CONSISTENTLY so two documents of the SAME kind get the SAME structure. Lead with its "
        "most distinctive entity — the person, organization, or subject — then the key detail. Follow "
        "these patterns for common kinds, and keep the same spirit for others:\n"
        "    - CV/resume -> \"CV_<NOM>-<Prenom>\" — underscore after CV, family NAME in UPPERCASE "
        "(+ \"_<target role>\" if stated), e.g. CV_LOUVEL-Simon_Developpeur-web (a CV is a SERIES: "
        "set \"series\": true, category_path the kind folder \".../Employment/CV\")\n"
        "    - facture/bill -> \"Facture_<issuer>\" (+ object only if it adds information), e.g. Facture_EDF "
        "(NOT Facture_EDF-electricite: the issuer already implies it)\n"
        "    - bank statement -> \"Releve_<bank>\", e.g. Releve_BNP-Paribas\n"
        "    - meter reading / relevé de compteur -> \"Releve_<resource>\", e.g. Releve_eau, "
        "Releve_electricite, Releve_gaz (the RESOURCE measured, not the word \"compteur\"); two "
        "readings of the same resource get the SAME name\n"
        "    - attestation/certificat/diploma -> \"<Type>_<organisme>_<subject>\", e.g. "
        "Certificat_OpenClassrooms_Developpeur-web\n"
        "  SEPARATORS: an underscore separates the name's semantic COMPONENTS (kind, issuer/person, "
        "subject); hyphens join the words WITHIN a component — e.g. Facture_EDF, Releve_BNP-Paribas, "
        "Constat-amiable_Degats-eaux-cuisine.\n"
        "  Rules: do NOT put a DATE in the name (the filename already carries the date); do NOT add words "
        "already implied by the entity; do NOT name it by its file type or format; avoid empty words like "
        "\"document\", \"fichier\", \"texte\". When the user context gives the person's identity, use it "
        "(e.g. a CV -> that exact Nom-Prenom).\n"
        "- \"date\": the document's own date (letter/invoice/statement date) as YYYY-MM-DD if clearly "
        "stated in the content; otherwise null.\n"
        "- \"category_path\": MUST start with one of these existing base categories "
        "(you may NOT invent a new top-level category):\n"
        f"{bases}\n"
        "  Reuse an EXISTING folder ONLY when the document is MANIFESTLY the same subject as what that "
        "folder already holds; reuse its exact path then. Otherwise create a new sibling subfolder — do "
        "NOT drop a document into an existing subfolder just because both sit under the same broad "
        "category (e.g. two unrelated Hobbies topics stay SEPARATE folders, never nested one inside the "
        "other). If confident about the base but unsure about the subfolder, return just the base — "
        "EXCEPT for a series, whose entity subfolder is REQUIRED (see SERIES RULE). If you truly cannot "
        "tell, set it to null.\n"
        "  SUBJECT-FIRST under a broad personal base: under a wide bucket like Hobbies, LEAD with a "
        "SUBJECT subfolder named for the document's topic (e.g. .../Hobbies/Musique, "
        ".../Hobbies/Jardinage), then any finer folder UNDER it. Infer the subject from the CONTENT; the "
        "user's stated interests are a GUIDE for naming/consistency, NOT a closed list — a document "
        "about a hobby they did not declare still gets its own subject subfolder. Never put an equipment "
        "or one-off folder DIRECTLY under the broad base: audio gear AND a music event both belong under "
        ".../Hobbies/Musique, not directly under Hobbies.\n"
        "  NOT A CATCH-ALL: only file under .../Hobbies/<subject> (or any existing subject folder) when "
        "the CONTENT is clearly about that subject. The mere EXISTENCE of such a folder in the tree is "
        "NOT a reason to send an unrelated file there — an avatar image, a chat screenshot, a random "
        "note do NOT become Music just because a Musique folder exists. When a document fits no clear "
        "subject and is no series, file it in the catch-all \"<base>/Misc\" (e.g. Personal/Misc, "
        "Work/Misc) rather than forcing it under an unrelated existing folder — but use Misc only "
        "as a LAST RESORT, when you genuinely cannot name a subject; a clearly-themed document still "
        "gets its own subject folder.\n"
        "  RECORD vs PUBLISHED CONTENT (judge by INTENT, not form — the key for images): a personal "
        "RECORD — a photo of a real moment, a scan of a paper — is filed by its subject (a photography "
        "hobby photo → Hobbies/Photo; a captured document → its admin subject). But CONTENT MADE FOR AN "
        "AUDIENCE — a designed or AI-generated visual, a meme, an infographic, a comparison, an "
        "announcement, a post, an avatar, a screenshot of a social post — belongs under "
        "\"Personal/Social-media\". An image is NOT \"Photo\" merely because it is an image: Photo is for "
        "actual photography, never a catch-all for made-to-publish graphics. If the context says the user "
        "creates/publishes content, lean that way; when such content is clearly the user's declared JOB "
        "or business it is Work instead. And when you can tell it is published/social content but "
        "genuinely CANNOT tell Personal from Work from the content and context, do NOT guess: set "
        "category_path null and offer BOTH Personal/Social-media and the matching Work folder as "
        "alternatives, so the user decides.\n"
        "  SERIES RULE: if the document is of an obviously RECURRING kind (meter reading, bank "
        "statement, payslip, bill, tax notice, insurance policy, CV, certificate/attestation/"
        "diploma…), set \"series\": true and make "
        "category_path the document's ENTITY folder — its issuer/organism (e.g. .../Utilities/EDF, "
        ".../Utilities/Enercoop, .../Banking/BNP-Paribas, .../Education/OpenClassrooms) or, when "
        "there is no issuer, the kind itself (e.g. .../Utilities/Releves-eau, .../Employment/CV). This entity subfolder is "
        "REQUIRED — NEVER stop at the bare base for a series, and also put the issuer in "
        "\"entities\".\"issuer\". Do NOT add a year — the system appends the dated year subfolder "
        "itself from the document's date. Documents from DIFFERENT entities are DIFFERENT series and "
        "go to DIFFERENT folders (an EDF bill and an Enercoop bill NEVER share a folder, even though "
        "both are electricity bills). REUSE an existing entity folder from the tree below when one "
        "already fits the SAME issuer.\n"
        "  NON-SERIES (\"series\": false): everything else. A one-off AFFAIR/event keeps its date in "
        "the FOLDER name, at the START (e.g. \".../Housing/2025-08_Degats-eaux-cuisine\"), never at "
        "the end. NEVER put a year in a category_path yourself — for a series the system adds it; "
        "elsewhere it does not belong.\n"
        "- \"series\": true only for an obviously recurring kind (see SERIES RULE); false otherwise.\n"
        "- \"alternatives\": up to 3 other plausible category paths (each under a base). Always provide "
        "some when category_path is null or you are unsure.\n"
        f"- \"summary\": {summary_instruction} on what the document is and its key point.\n"
        f"- \"keywords\": {keywords_instruction}.\n"
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
    user_language: str = "en",
    sibling_filenames: list[str] | None = None,
    read_via: str | None = None,
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

    `read_via` says HOW the text was obtained (`text` / `ocr` / `vision`) and so how
    far to trust it against the filesystem hints; `sibling_filenames` are the other
    files dropped in the same set. See `_build_hints_block`.
    """
    chain_entries = chain if chain is not None else task_chain_from_env("ANALYSIS")
    if not chain_entries:
        return _empty_result(provider="none", model="none", reason="chain_not_configured")
    if not text.strip():
        return _empty_result(provider="none", model="none", reason="no_content")

    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("ANALYSIS", default_value=60, provider=chain_entries[0].provider)
    retry_count = retries if retries is not None else _task_retries_from_env("ANALYSIS", default_value=2)
    prompt = _build_analysis_prompt(
        text, base_categories, existing_paths, original_filename, source_folder, user_context,
        user_language, sibling_filenames=sibling_filenames, read_via=read_via,
    )

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

                payload = _extract_json_dict(raw_output)
                if payload is None:
                    raise ProviderCallError("INVALID_JSON_RESPONSE")

                entities = payload.get("entities")
                return AnalysisResult(
                    name=_clean_stem(payload.get("name")),
                    document_date=_extract_document_date(payload),
                    category_path=_clean_path(payload.get("category_path")),
                    series=bool(payload.get("series")),
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


def _build_translate_prompt(keywords: list[str], summary: str | None, language_name: str) -> str:
    context = f"\nThe document's summary, for context: {summary}\n" if summary else ""
    return (
        "Expand these document keywords for full-text search. Return short, lowercase search terms "
        f"covering the SAME meaning in BOTH English and {language_name}, including close synonyms in "
        "each language. Keep proper nouns as-is. Return JSON only: {\"keywords\": [\"...\"]}.\n"
        f"Existing keywords: {', '.join(keywords)}\n{context}"
    )


def translate_keywords(
    keywords: list[str],
    *,
    language: str,
    summary: str | None = None,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> list[str]:
    """Ask the AI for the keywords' equivalents + synonyms in English AND
    `language` (a short code), for search. Returns [] when there is no chain, no
    keywords, English-only (nothing to translate to), or every provider fails —
    the caller then leaves the fiche unchanged."""
    code = (language or "en").lower()
    if code == "en" or not keywords:
        return []
    chain_entries = chain if chain is not None else task_chain_from_env("ANALYSIS")
    if not chain_entries:
        return []

    prompt = _build_translate_prompt(keywords, summary, _LANG_NAMES.get(code, code))
    return _keywords_from_chain(prompt, chain_entries, timeout_seconds, retries, sleep_fn)


def _keywords_from_chain(
    prompt: str, chain_entries: list[ChainEntry],
    timeout_seconds: int | None, retries: int | None, sleep_fn: Any,
) -> list[str]:
    """Run a prompt through the chain and return the JSON `keywords` list, or []
    on total failure. Shared by `translate_keywords` and `expand_query`."""
    timeout = timeout_seconds if timeout_seconds is not None else _task_timeout_from_env("ANALYSIS", default_value=60, provider=chain_entries[0].provider)
    retry_count = retries if retries is not None else _task_retries_from_env("ANALYSIS", default_value=2)
    for entry in chain_entries:
        for attempt in range(retry_count + 1):
            try:
                if entry.provider == "mistral":
                    raw_output = call_mistral_chat(prompt, entry.model, timeout=timeout)
                elif entry.provider == "ollama":
                    raw_output = call_ollama_chat(prompt, entry.model, timeout=timeout)
                else:
                    raise ProviderCallError(f"unsupported_provider:{entry.provider}")
                payload = _extract_json_dict(raw_output)
                if payload is None:
                    raise ProviderCallError("INVALID_JSON_RESPONSE")
                return _clean_keyword_list(payload.get("keywords"))
            except (RateLimitedError, ProviderCallError):
                if attempt < retry_count:
                    sleep_fn(2**attempt)
                    continue
                break
    return []


def _build_expand_query_prompt(query: str, language_name: str) -> str:
    return (
        "Broaden this search query into related search terms — synonyms, and translations into "
        f"both English and {language_name} — so a full-text search finds more relevant documents. "
        "Short, lowercase terms; keep proper nouns as-is. Return JSON only: {\"keywords\": [\"...\"]}.\n"
        f"Search query: {query}"
    )


def expand_query(
    query: str,
    *,
    language: str,
    chain: list[ChainEntry] | None = None,
    timeout_seconds: int | None = None,
    retries: int | None = None,
    sleep_fn: Any = time.sleep,
) -> list[str]:
    """Ask the AI for terms related to a search query — synonyms and English/`language`
    translations — to broaden a search (powers `search-ai`). Returns [] when there is
    no chain, an empty query, or every provider fails. Works for English too (synonyms)."""
    text = query.strip()
    chain_entries = chain if chain is not None else task_chain_from_env("ANALYSIS")
    if not text or not chain_entries:
        return []
    name = _LANG_NAMES.get((language or "en").lower(), language or "en")
    prompt = _build_expand_query_prompt(text, name)
    return _keywords_from_chain(prompt, chain_entries, timeout_seconds, retries, sleep_fn)
