"""Local, offline search over the catalog fiche (SQLite FTS5).

Search never re-reads the documents and never calls an AI: it queries the fiche
already stored per document (name, keywords, entities, summary) — the whole point
of having built the catalog. This is Slice 1 ("find by what it is"); indexing the
documents' full body text for deep word search is a later slice.

A temporary FTS5 table is built from the catalog at query time, so results are
always consistent with the catalog and there is no index to migrate or keep in
sync. For a few thousand documents this is milliseconds; a persistent/incremental
index is an optimisation for very large libraries, not a correctness need.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Accent-insensitive, Unicode-aware tokenizer: "impot" matches "impôt" (typed
# either way), which is kinder for accessibility and quick typing.
_TOKENIZER = "unicode61 remove_diacritics 2"

# BM25 column weights (0 = ignored). Order matches the FTS columns below: the
# name and keywords are the strongest signal of what a document IS, the summary
# the weakest. doc_id is UNINDEXED (weight 0).
_BM25_WEIGHTS = (0.0, 5.0, 4.0, 3.0, 1.0)


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
    """Return the documents whose fiche matches `query`, best first (BM25)."""
    match = _fts_match_query(query)
    if not match:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp.search_fts USING fts5("
            f"doc_id UNINDEXED, name, keywords, entities, summary, tokenize='{_TOKENIZER}')"
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
            conn.execute(
                "INSERT INTO temp.search_fts(doc_id, name, keywords, entities, summary) VALUES (?, ?, ?, ?, ?)",
                (row["doc_id"], name, keywords_text, entities_text, summary),
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
