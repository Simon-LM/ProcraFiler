from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class CatalogRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    current_filename TEXT NOT NULL,
                    current_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256)")

            existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
            if "flow_state" not in existing_columns:
                conn.execute("ALTER TABLE documents ADD COLUMN flow_state TEXT")
            if "pending_decision" not in existing_columns:
                # JSON blob describing a parked decision (options + reason +
                # snippet) for files awaiting `procrafiler review`. NULL otherwise.
                conn.execute("ALTER TABLE documents ADD COLUMN pending_decision TEXT")
            if "content_json" not in existing_columns:
                # The document fiche (spec §4.1) as a JSON string: name, date,
                # category_path, alternatives, summary, keywords, entities,
                # language, provenance. Produced once at read time; powers search
                # and reorganization without re-reading the file. NULL when the
                # analysis step could not run.
                conn.execute("ALTER TABLE documents ADD COLUMN content_json TEXT")
            if "last_verified_utc" not in existing_columns:
                # ISO-8601 UTC of the last time the integrity scrub re-hashed this
                # document and it matched the catalog sha256. NULL = never verified
                # (scrubbed first). See docs/durability.md.
                conn.execute("ALTER TABLE documents ADD COLUMN last_verified_utc TEXT")
            conn.commit()

    def upsert_document(
        self,
        *,
        doc_id: str,
        sha256: str,
        current_filename: str,
        current_path: str,
        status: str,
        updated_at_utc: str,
        flow_state: str | None = None,
        pending_decision: str | None = None,
        content_json: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (
                    doc_id, sha256, current_filename, current_path, status, updated_at_utc,
                    flow_state, pending_decision, content_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    sha256=excluded.sha256,
                    current_filename=excluded.current_filename,
                    current_path=excluded.current_path,
                    status=excluded.status,
                    updated_at_utc=excluded.updated_at_utc,
                    flow_state=excluded.flow_state,
                    pending_decision=excluded.pending_decision,
                    content_json=excluded.content_json
                """,
                (
                    doc_id, sha256, current_filename, current_path, status, updated_at_utc,
                    flow_state, pending_decision, content_json,
                ),
            )
            conn.commit()

    def list_pending_decisions(self) -> list[dict[str, str | None]]:
        """Documents parked for `review` (status DECISION_PENDING), oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT doc_id, sha256, current_filename, current_path, status, updated_at_utc,
                       flow_state, pending_decision, content_json
                FROM documents
                WHERE status = 'DECISION_PENDING'
                ORDER BY updated_at_utc ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def has_sha256(self, sha256: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM documents WHERE sha256 = ? LIMIT 1", (sha256,)).fetchone()
            return row is not None

    def has_live_sha256(self, sha256: str) -> bool:
        """True only when a NON-deleted document has this content — so a deleted
        document's tombstone never makes a re-deposit look like a duplicate."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM documents WHERE sha256 = ? AND status != 'DELETED' LIMIT 1",
                (sha256,),
            ).fetchone()
            return row is not None

    def deleted_at_for_sha256(self, sha256: str) -> str | None:
        """The deletion date of a tombstone with this content (most recent), or
        None — lets the pipeline tell the user a re-deposited file was deleted before."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT updated_at_utc FROM documents WHERE sha256 = ? AND status = 'DELETED' "
                "ORDER BY updated_at_utc DESC LIMIT 1",
                (sha256,),
            ).fetchone()
            return str(row["updated_at_utc"]) if row else None

    def tombstone_document(self, doc_id: str, *, sha256: str, deleted_at: str) -> None:
        """Reduce a row to a TOMBSTONE — keep only id + hash + deletion date, drop
        every other detail (name, path, fiche). Enough to recognise a re-deposit,
        nothing of the document's content lingers."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET current_filename = '', current_path = '', status = 'DELETED', "
                "updated_at_utc = ?, flow_state = NULL, pending_decision = NULL, content_json = NULL "
                "WHERE doc_id = ?",
                (deleted_at, doc_id),
            )
            conn.commit()

    def purge_document(self, doc_id: str) -> None:
        """Remove a document's row entirely — no id, hash or fiche kept (purge
        deletion mode). The deletion survives only in the action log, by design;
        a later re-deposit is NOT recognised (nothing remains to match it)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            conn.commit()

    def majority_language(self) -> str | None:
        """The language most documents are in — the AI records each document's
        language in its fiche, so the app can work in the user's language with no
        configuration. None when there is nothing to go on (empty/absent catalog)."""
        counts: dict[str, int] = {}
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT content_json FROM documents "
                    "WHERE status = 'LIBRARY_STORED' AND content_json IS NOT NULL"
                ).fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            try:
                fiche = json.loads(row["content_json"])
            except (TypeError, ValueError):
                continue
            language = fiche.get("language") if isinstance(fiche, dict) else None
            if isinstance(language, str) and language.strip():
                code = language.strip().lower()
                counts[code] = counts.get(code, 0) + 1
        if not counts:
            return None
        return max(counts, key=lambda code: counts[code])

    def find_by_current_path(self, current_path: str) -> dict[str, str | None] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT doc_id, sha256, current_filename, current_path, status, updated_at_utc,
                       flow_state, content_json
                FROM documents
                WHERE current_path = ?
                LIMIT 1
                """,
                (current_path,),
            ).fetchone()
            return dict(row) if row else None

    def list_documents(self) -> list[dict[str, str | None]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT doc_id, sha256, current_filename, current_path, status, updated_at_utc,
                       flow_state, content_json
                FROM documents
                ORDER BY updated_at_utc DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def integrity_ok(self) -> bool:
        """True if SQLite's `integrity_check` passes (the DB is structurally sound).
        A DB so damaged it cannot be opened/queried returns False."""
        try:
            with self._connect() as conn:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError:
            return False
        return len(rows) == 1 and str(rows[0][0]).lower() == "ok"

    def count_documents(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])

    def documents_for_scrub(self, *, limit: int | None = None) -> list[dict[str, str | None]]:
        """Stored library documents to verify, least-recently-verified first
        (NULL `last_verified_utc` = never checked → sorts first in SQLite ASC)."""
        query = (
            "SELECT doc_id, sha256, current_filename, current_path, last_verified_utc "
            "FROM documents WHERE status = 'LIBRARY_STORED' "
            "ORDER BY last_verified_utc ASC"
        )
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def content_confirmed_timestamps(self) -> list[str]:
        """When each stored document's content was last confirmed, as recorded.

        `last_verified_utc` once a scrub has checked it, else `updated_at_utc`: at
        filing the sha256 was computed FROM the file, so that moment did confirm it.
        One expression for both keeps every document in the same count — a document
        filed yesterday is not overdue, and one filed two years ago and never
        re-checked is. A separate "never verified" bucket would put exactly those
        stale documents outside the rule meant to cover them.

        Returned raw, and compared in Python on purpose: the two columns are written
        in DIFFERENT shapes — `updated_at_utc` as `…:00Z`, `last_verified_utc` as
        `…:00+00:00` — so ordering them as TEXT in SQL would rank the same instant
        differently depending on which column it came from.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(last_verified_utc, updated_at_utc) AS confirmed "
                "FROM documents WHERE status = 'LIBRARY_STORED'"
            ).fetchall()
        return [str(row["confirmed"]) for row in rows if row["confirmed"]]

    def mark_verified(self, doc_ids: list[str], *, when_utc: str) -> None:
        """Record that these documents' content matched the catalog at `when_utc`."""
        if not doc_ids:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE documents SET last_verified_utc = ? WHERE doc_id = ?",
                [(when_utc, doc_id) for doc_id in doc_ids],
            )
            conn.commit()
