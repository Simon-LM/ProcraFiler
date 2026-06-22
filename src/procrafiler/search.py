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

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from procrafiler.content_reader import extract_text_content
from procrafiler.taxonomy import dispatch_for_filename

# Accent-insensitive, Unicode-aware tokenizer: "impot" matches "impôt" (typed
# either way), which is kinder for accessibility and quick typing.
_TOKENIZER = "unicode61 remove_diacritics 2"

# BM25 column weights (0 = ignored). Order matches the FTS columns below: the
# name and keywords are the strongest signal of what a document IS, the body
# (content) is deep-search recall, the summary the weakest. doc_id is UNINDEXED.
_BM25_WEIGHTS = (0.0, 5.0, 4.0, 3.0, 1.0, 2.0)

# Cap the body text indexed per document — bounds memory/time at query time and
# is far more than enough for full-text recall on a single document.
_MAX_BODY_CHARS = 100_000


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


def _fts_match_query(raw: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression: each term quoted
    (so punctuation/accents can't break the syntax) and AND-ed together."""
    terms = [t for t in raw.split() if t.strip()]
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _fiche(content_json: Any) -> dict[str, Any]:
    if not content_json:
        return {}
    try:
        parsed = json.loads(content_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def search_catalog(db_path: Path, query: str, *, limit: int = 20) -> list[SearchHit]:
    """Return the documents whose fiche OR body text matches `query`, best first (BM25)."""
    match = _fts_match_query(query)
    if not match:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.search_fts USING fts5("
            f"doc_id UNINDEXED, name, keywords, entities, summary, content, tokenize='{_TOKENIZER}')"
        )
        meta: dict[str, tuple[str, str | None, str | None, str]] = {}
        for row in conn.execute(
            "SELECT doc_id, current_filename, current_path, content_json "
            "FROM documents WHERE status = 'LIBRARY_STORED'"
        ):
            fiche = _fiche(row["content_json"])
            name = fiche.get("name") or Path(str(row["current_filename"])).stem
            keywords = fiche.get("keywords")
            keywords_text = " ".join(keywords) if isinstance(keywords, list) else ""
            entities = fiche.get("entities")
            entities_text = " ".join(str(v) for v in entities.values()) if isinstance(entities, dict) else ""
            summary = fiche.get("summary") or ""
            content = _body_text(str(row["current_path"]))
            conn.execute(
                "INSERT INTO temp.search_fts(doc_id, name, keywords, entities, summary, content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row["doc_id"], name, keywords_text, entities_text, summary, content),
            )
            meta[str(row["doc_id"])] = (
                name,
                fiche.get("category_path"),
                fiche.get("effective_date") or fiche.get("document_date"),
                str(row["current_path"]),
            )

        weights = ", ".join(str(w) for w in _BM25_WEIGHTS)
        hits: list[SearchHit] = []
        for row in conn.execute(
            "SELECT doc_id, snippet(search_fts, -1, '[', ']', '…', 10) AS snip "
            "FROM temp.search_fts WHERE search_fts MATCH ? "
            f"ORDER BY bm25(search_fts, {weights}) LIMIT ?",
            (match, limit),
        ):
            name, category, date, path = meta[str(row["doc_id"])]
            hits.append(SearchHit(str(row["doc_id"]), name, category, date, path, str(row["snip"] or "")))
        return hits
    finally:
        conn.close()
