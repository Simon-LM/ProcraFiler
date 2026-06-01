from __future__ import annotations

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
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (
                    doc_id, sha256, current_filename, current_path, status, updated_at_utc,
                    flow_state, pending_decision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    sha256=excluded.sha256,
                    current_filename=excluded.current_filename,
                    current_path=excluded.current_path,
                    status=excluded.status,
                    updated_at_utc=excluded.updated_at_utc,
                    flow_state=excluded.flow_state,
                    pending_decision=excluded.pending_decision
                """,
                (doc_id, sha256, current_filename, current_path, status, updated_at_utc, flow_state, pending_decision),
            )
            conn.commit()

    def list_pending_decisions(self) -> list[dict[str, str | None]]:
        """Documents parked for `review` (status DECISION_PENDING), oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT doc_id, sha256, current_filename, current_path, status, updated_at_utc,
                       flow_state, pending_decision
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

    def find_by_current_path(self, current_path: str) -> dict[str, str | None] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT doc_id, sha256, current_filename, current_path, status, updated_at_utc, flow_state
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
                SELECT doc_id, sha256, current_filename, current_path, status, updated_at_utc, flow_state
                FROM documents
                ORDER BY updated_at_utc DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]
