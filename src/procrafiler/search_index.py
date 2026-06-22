"""Persistent body-text index for content search (Search Slice 4).

Deep content search (Slice 3) reads each document's body on disk at query time —
cheap for sidecars and text files, but it re-extracts PDFs on every query. This
caches the extracted body text in a dedicated SQLite file (`search_index.db`),
keyed by the document's content fingerprint (sha256):

- keyed by **content**, so a moved/renamed file never invalidates its body and
  duplicates share one entry;
- a **dedicated** store, so the main catalog stays lean;
- **self-warming** (search caches what it reads) and bulk-fillable / prunable via
  the `reindex` command (the backfill);
- **pruned on deletion**, so a purged/tombstoned document's content does not
  linger in the index.

An empty string is a valid cached body ("checked, nothing locally readable") — it
stops a scanned PDF being re-extracted on every query.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path


class BodyTextIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS body ("
                "sha256 TEXT PRIMARY KEY, content TEXT NOT NULL, indexed_at_utc TEXT NOT NULL)"
            )
            conn.commit()

    def get_many(self, shas: Iterable[str]) -> dict[str, str]:
        unique = [s for s in dict.fromkeys(shas) if s]
        if not unique:
            return {}
        out: dict[str, str] = {}
        with self._connect() as conn:
            for start in range(0, len(unique), 500):  # stay under SQLite's variable limit
                chunk = unique[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    f"SELECT sha256, content FROM body WHERE sha256 IN ({placeholders})", chunk
                ):
                    out[str(row["sha256"])] = str(row["content"])
        return out

    def put(self, sha256: str, content: str, *, now_utc_iso: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO body(sha256, content, indexed_at_utc) VALUES (?, ?, ?) "
                "ON CONFLICT(sha256) DO UPDATE SET content=excluded.content, indexed_at_utc=excluded.indexed_at_utc",
                (sha256, content, now_utc_iso),
            )
            conn.commit()

    def delete(self, sha256: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM body WHERE sha256 = ?", (sha256,))
            conn.commit()

    def all_shas(self) -> set[str]:
        with self._connect() as conn:
            return {str(r["sha256"]) for r in conn.execute("SELECT sha256 FROM body")}

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS c FROM body").fetchone()["c"])

    def prune(self, keep_shas: Iterable[str]) -> int:
        """Drop entries whose sha is not in `keep_shas` (orphans). Returns how many were removed."""
        keep = set(keep_shas)
        with self._connect() as conn:
            orphans = [str(r["sha256"]) for r in conn.execute("SELECT sha256 FROM body") if str(r["sha256"]) not in keep]
            conn.executemany("DELETE FROM body WHERE sha256 = ?", [(s,) for s in orphans])
            conn.commit()
        return len(orphans)
