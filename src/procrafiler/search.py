"""Local, offline search over the catalog — fiche AND document body (SQLite FTS5).

Search never calls an AI: it queries the fiche already stored per document (name,
keywords, entities, summary) AND the document's body text (Slice 3, "find by what
it SAYS"). The body text is read on disk with no AI and no network:

- from the hidden text sidecar (`.<filename>.txt`) when one exists — the costly
  OCR/vision text was extracted once and cached there (Slice 2);
- otherwise straight from a locally-readable file (a plain-text file, or a PDF
  with a real text layer) via the same reader the pipeline uses.

A scanned page / image with no sidecar contributes only its fiche (we never OCR at
query time). A temporary FTS5 table is built from the catalog at query time, so
results are always consistent with the catalog and there is no index to migrate.
For a few thousand documents this is fast; a persistent/incremental content index
(so PDFs aren't re-read each query) is the next slice — an optimisation for very
large libraries, not a correctness need.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from procrafiler.content_reader import extract_text_content
from procrafiler.search_index import BodyTextIndex
from procrafiler.taxonomy import dispatch_for_filename, folder_synonyms

# Accent-insensitive, Unicode-aware tokenizer: "impot" matches "impôt" (typed
# either way), which is kinder for accessibility and quick typing.
_TOKENIZER = "unicode61 remove_diacritics 2"

# Function words dropped from an OR query (search-ai). They carry no search
# meaning but, OR-ed, would match their grammatical occurrences everywhere — e.g.
# the French possessive "son" (his/her) would match every "son nom"/"son dossier",
# drowning the real "son" = sound hits. Multi-word terms (phrases) are never
# dropped. Accent-stripped to match the tokenizer.
_STOPWORDS = frozenset((
    # French
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "l",
    "et", "ou", "ni", "mais", "donc", "car", "que", "qui", "quoi", "dont",
    "a", "au", "aux", "en", "dans", "sur", "sous", "par", "pour", "avec", "sans",
    "chez", "vers", "entre", "ce", "cet", "cette", "ces",
    "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa", "ses",
    "notre", "nos", "votre", "vos", "leur", "leurs",
    "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles",
    "est", "sont", "etre", "ont", "avoir", "plus", "moins", "tres", "pas", "ne",
    # English
    "the", "a", "an", "of", "to", "in", "on", "for", "with", "without",
    "and", "or", "nor", "but", "is", "are", "be", "been", "this", "that", "these", "those",
    "his", "her", "its", "their", "our", "your", "my", "it", "as", "at", "by", "from",
))


def _is_stopword(term: str) -> bool:
    return " " not in term and term.strip().lower() in _STOPWORDS

# BM25 column weights (0 = ignored). Order matches the FTS columns below: the
# name and keywords are the strongest signal of what a document IS, the body
# (content) and category are deep-search/cross-language recall, the summary the
# weakest. doc_id is UNINDEXED.
_BM25_WEIGHTS = (0.0, 5.0, 4.0, 3.0, 1.0, 2.0, 2.0)


def _category_terms(category_path: str | None, user_language: str) -> str:
    """The category's folder names PLUS their synonyms/translations in the user's
    language — so a document is findable by its category in English and in the
    user's language (e.g. `Hobbies` is found by `loisirs`/`passion`). No AI."""
    if not category_path:
        return ""
    terms: list[str] = []
    for segment in str(category_path).replace("\\", "/").split("/"):
        seg = segment.strip()
        if not seg:
            continue
        terms.append(seg)
        terms.extend(folder_synonyms(seg, user_language))
    return " ".join(terms)

# Cap the body text indexed per document — bounds memory/time at query time and
# is far more than enough for full-text recall on a single document.
_MAX_BODY_CHARS = 100_000


def _default_index_path(db_path: Path) -> Path:
    return db_path.parent / "search_index.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sidecar_path(doc_path: Path) -> Path:
    return doc_path.parent / ("." + doc_path.name + ".txt")


def _body_text(current_path: str) -> str:
    """The document's body text for indexing — sidecar first (cached OCR/vision),
    else read locally (plain text / readable PDF). No AI, no network; '' when
    there is nothing locally readable (e.g. a scanned page with no sidecar)."""
    doc_path = Path(current_path)
    sidecar = _sidecar_path(doc_path)
    if sidecar.is_file():
        try:
            return sidecar.read_text(encoding="utf-8", errors="ignore")[:_MAX_BODY_CHARS]
        except OSError:
            return ""
    if not doc_path.is_file():
        return ""
    media_type = dispatch_for_filename(doc_path.name).media_type
    if media_type is None:
        return ""
    try:
        extraction = extract_text_content(doc_path, media_type)
    except Exception:  # noqa: BLE001 — a single unreadable file must not break search
        return ""
    return (extraction.text or "")[:_MAX_BODY_CHARS]


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    name: str
    category_path: str | None
    date: str | None
    path: str
    snippet: str


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _fts_match_query(raw: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression: each term quoted
    (so punctuation/accents can't break the syntax) and AND-ed together."""
    return " ".join(_quote(t) for t in raw.split() if t.strip())


def _vocabulary(conn: sqlite3.Connection) -> list[str]:
    """All indexed terms (tokenized, accent-stripped) of the temp FTS table —
    used to suggest near-matches for a misspelled query, with no AI."""
    conn.execute("CREATE VIRTUAL TABLE temp.search_vocab USING fts5vocab('search_fts', 'row')")
    return [str(r["term"]) for r in conn.execute("SELECT term FROM temp.search_vocab")]


def _fuzzy_match_query(raw: str, vocab: list[str]) -> str | None:
    """A typo-tolerant MATCH expression: each query term is OR-ed with its closest
    indexed terms (edit-distance, offline). Returns None when nothing close is
    found (so the exact, empty result stands). E.g. `pasisons` -> `passions`."""
    if not vocab:
        return None
    groups: list[str] = []
    widened = False
    for term in (t for t in raw.split() if t.strip()):
        low = term.lower()
        variants = list(dict.fromkeys([low, *difflib.get_close_matches(low, vocab, n=5, cutoff=0.8)]))
        if any(v != low for v in variants):
            widened = True
        groups.append("(" + " OR ".join(_quote(v) for v in variants) + ")")
    return " ".join(groups) if widened else None


def _fiche(content_json: Any) -> dict[str, Any]:
    if not content_json:
        return {}
    try:
        parsed = json.loads(content_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cached_body(index: BodyTextIndex, cached: dict[str, str], sha256: str, current_path: str) -> str:
    """Body text for a document: from the persistent index when present, else read
    it once (Slice 3) and cache it by content hash so it isn't re-read next time."""
    if sha256 in cached:
        return cached[sha256]
    body = _body_text(current_path)
    cached[sha256] = body  # keep within this query (duplicates share a sha)
    if sha256:
        index.put(sha256, body, now_utc_iso=_now_iso())
    return body


def _search(
    db_path: Path, *, limit: int, index_path: Path | None, user_language: str,
    strategy: "Callable[[sqlite3.Connection, Callable[[str], list[SearchHit]]], list[SearchHit]]",
) -> list[SearchHit]:
    """Build the query-time FTS table from the catalog (fiche + body + category)
    once, then let `strategy(conn, run)` decide which MATCH expression(s) to run.
    Shared by `search_catalog` (exact + typo fallback) and `search_catalog_any`."""
    index = BodyTextIndex(index_path or _default_index_path(db_path))
    index.init_schema()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(
            "SELECT doc_id, sha256, current_filename, current_path, content_json "
            "FROM documents WHERE status = 'LIBRARY_STORED'"
        ))
        cached = index.get_many(str(r["sha256"]) for r in rows)

        conn.execute(
            "CREATE VIRTUAL TABLE temp.search_fts USING fts5("
            f"doc_id UNINDEXED, name, keywords, entities, summary, content, category, "
            f"tokenize='{_TOKENIZER}')"
        )
        meta: dict[str, tuple[str, str | None, str | None, str]] = {}
        for row in rows:
            fiche = _fiche(row["content_json"])
            name = fiche.get("name") or Path(str(row["current_filename"])).stem
            keywords = fiche.get("keywords")
            keywords_text = " ".join(keywords) if isinstance(keywords, list) else ""
            entities = fiche.get("entities")
            entities_text = " ".join(str(v) for v in entities.values()) if isinstance(entities, dict) else ""
            summary = fiche.get("summary") or ""
            content = _cached_body(index, cached, str(row["sha256"]), str(row["current_path"]))
            category = _category_terms(fiche.get("category_path"), user_language)
            conn.execute(
                "INSERT INTO temp.search_fts(doc_id, name, keywords, entities, summary, content, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["doc_id"], name, keywords_text, entities_text, summary, content, category),
            )
            meta[str(row["doc_id"])] = (
                name,
                fiche.get("category_path"),
                fiche.get("effective_date") or fiche.get("document_date"),
                str(row["current_path"]),
            )

        weights = ", ".join(str(w) for w in _BM25_WEIGHTS)

        def run(match_expr: str) -> list[SearchHit]:
            out: list[SearchHit] = []
            for row in conn.execute(
                "SELECT doc_id, snippet(search_fts, -1, '[', ']', '…', 10) AS snip "
                "FROM temp.search_fts WHERE search_fts MATCH ? "
                f"ORDER BY bm25(search_fts, {weights}) LIMIT ?",
                (match_expr, limit),
            ):
                name, category, date, path = meta[str(row["doc_id"])]
                out.append(SearchHit(str(row["doc_id"]), name, category, date, path, str(row["snip"] or "")))
            return out

        return strategy(conn, run)
    finally:
        conn.close()


def search_catalog(
    db_path: Path, query: str, *, limit: int = 20, index_path: Path | None = None,
    user_language: str = "en",
) -> list[SearchHit]:
    """Return the documents whose fiche, body text OR category matches `query`,
    best first (BM25). `user_language` lets the category be matched in the user's
    language as well as English (e.g. `loisirs` finds `Hobbies`). Typo-tolerant:
    when nothing matches exactly, query terms fall back to their closest indexed
    terms (offline, no AI)."""
    match = _fts_match_query(query)
    if not match:
        return []

    def strategy(conn: sqlite3.Connection, run: "Callable[[str], list[SearchHit]]") -> list[SearchHit]:
        hits = run(match)
        if hits:
            return hits
        fuzzy = _fuzzy_match_query(query, _vocabulary(conn))
        return run(fuzzy) if fuzzy else []

    return _search(db_path, limit=limit, index_path=index_path, user_language=user_language, strategy=strategy)


def search_catalog_any(
    db_path: Path, terms: list[str], *, limit: int = 20, index_path: Path | None = None,
    user_language: str = "en",
) -> list[SearchHit]:
    """Return documents matching ANY of `terms` (OR), best first (BM25). Powers
    `search-ai`, which feeds it a query broadened with AI synonyms/translations.
    Function words (`son`, `de`, `the`…) are dropped so a common grammatical term
    in the set doesn't drown the real hits; multi-word phrases are kept."""
    cleaned = [t.strip() for t in terms if t and t.strip() and not _is_stopword(t)]
    if not cleaned:
        return []
    match = "(" + " OR ".join(_quote(t) for t in cleaned) + ")"
    return _search(
        db_path, limit=limit, index_path=index_path, user_language=user_language,
        strategy=lambda _conn, run: run(match),
    )


def reindex_content(db_path: Path, *, index_path: Path | None = None) -> dict[str, int]:
    """Backfill: make the persistent body index match the live library exactly —
    extract + cache the body of every filed document not yet indexed, and drop
    entries for content no longer present. Returns {indexed, added, pruned}.
    Safe to re-run; only missing bodies are read (so re-runs are fast)."""
    index = BodyTextIndex(index_path or _default_index_path(db_path))
    index.init_schema()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(
            "SELECT sha256, current_path FROM documents WHERE status = 'LIBRARY_STORED'"
        ))
    finally:
        conn.close()

    live_shas = {str(r["sha256"]) for r in rows if r["sha256"]}
    path_for_sha: dict[str, str] = {}
    for r in rows:
        path_for_sha.setdefault(str(r["sha256"]), str(r["current_path"]))

    now_iso = _now_iso()
    added = 0
    for sha in live_shas - index.all_shas():
        index.put(sha, _body_text(path_for_sha[sha]), now_utc_iso=now_iso)
        added += 1
    pruned = index.prune(live_shas)
    return {"indexed": index.count(), "added": added, "pruned": pruned}
