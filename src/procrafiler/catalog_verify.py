"""Catalog durability (durability Phase 1, see docs/durability.md).

The catalog metadata is as precious as the files: losing it loses search, dedup
tombstones and provenance. `verify-catalog` checks the SQLite DB
(`PRAGMA integrity_check`) and, when it is corrupt or empty-but-recoverable,
rebuilds it from the atomic `catalog_snapshot.json` (the corruption-resistant
plain-text backup). The old DB is moved aside, never deleted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import RuntimePaths


@dataclass
class CatalogVerifyReport:
    db_present: bool
    integrity_ok: bool
    db_count: int | None  # None when the DB can't be read
    snapshot_present: bool
    snapshot_count: int | None
    rebuilt: bool = False
    rebuilt_count: int = 0
    backup_path: str | None = None

    @property
    def _db_has_data(self) -> bool:
        return (self.db_count or 0) > 0

    @property
    def _snapshot_has_data(self) -> bool:
        return (self.snapshot_count or 0) > 0

    @property
    def healthy(self) -> bool:
        # Healthy = sound DB that still holds its data, OR a sound empty DB with
        # nothing in the snapshot to recover (a fresh install).
        return self.integrity_ok and (self._db_has_data or not self._snapshot_has_data)

    @property
    def needs_recovery(self) -> bool:
        return not self.healthy

    @property
    def recoverable(self) -> bool:
        return self.needs_recovery and self._snapshot_has_data

    @property
    def ok(self) -> bool:
        return self.healthy or self.rebuilt


def _read_snapshot_documents(path: Path) -> list[dict] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    docs = data.get("documents") if isinstance(data, dict) else None
    return docs if isinstance(docs, list) else None


def _reroot(current_path: str, old_root: Path, new_root: Path) -> str:
    """Move an absolute library path from `old_root` to `new_root`, keeping the
    relative part. Leaves paths that aren't under `old_root` (e.g. tombstones with
    an empty path) unchanged."""
    if not current_path:
        return current_path
    try:
        rel = Path(current_path).relative_to(old_root)
    except ValueError:
        return current_path
    return str(new_root / rel)


def rebuild_catalog_from_snapshot(
    paths: RuntimePaths,
    snapshot_docs: list[dict],
    *,
    now_utc: str | None = None,
    reroot: tuple[Path, Path] | None = None,
) -> tuple[int, str]:
    """Rebuild a fresh `catalog.db` from the snapshot documents. The existing DB
    is moved aside first. When `reroot=(old_root, new_root)` is given, each
    document's `current_path` is moved from the old library location to the new
    one (used by `restore`). Returns (count, backup_path)."""
    db = paths.catalog_db_file
    stamp = (now_utc or datetime.now(timezone.utc).isoformat()).replace(":", "").replace("-", "")
    backup = db.with_name(db.name + f".corrupt-{stamp}")
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.replace(backup)
    repo = CatalogRepository(db)
    repo.init_schema()
    count = 0
    for doc in snapshot_docs:
        content = doc.get("content")
        content_json = json.dumps(content, ensure_ascii=False) if content else None
        current_path = str(doc.get("current_path", ""))
        if reroot is not None:
            current_path = _reroot(current_path, reroot[0], reroot[1])
        repo.upsert_document(
            doc_id=str(doc.get("doc_id")),
            sha256=str(doc.get("sha256", "")),
            current_filename=str(doc.get("current_filename", "")),
            current_path=current_path,
            status=str(doc.get("status", "")),
            updated_at_utc=str(doc.get("updated_at_utc", "")),
            flow_state=doc.get("flow_state"),
            content_json=content_json,
        )
        count += 1
    return count, str(backup)


def verify_catalog(
    paths: RuntimePaths, *, rebuild: bool = False, now_utc: str | None = None
) -> CatalogVerifyReport:
    db = paths.catalog_db_file
    repo = CatalogRepository(db)
    db_present = db.is_file()
    integrity = repo.integrity_ok() if db_present else False
    db_count: int | None = None
    if integrity:
        try:
            db_count = repo.count_documents()
        except Exception:
            db_count = None

    snapshot_docs = _read_snapshot_documents(paths.catalog_snapshot_file)
    report = CatalogVerifyReport(
        db_present=db_present,
        integrity_ok=integrity,
        db_count=db_count,
        snapshot_present=snapshot_docs is not None,
        snapshot_count=len(snapshot_docs) if snapshot_docs is not None else None,
    )

    if rebuild and report.recoverable and snapshot_docs is not None:
        count, backup = rebuild_catalog_from_snapshot(paths, snapshot_docs, now_utc=now_utc)
        report.rebuilt = True
        report.rebuilt_count = count
        report.backup_path = backup
    return report


def format_report(report: CatalogVerifyReport) -> str:
    if report.rebuilt:
        return (
            f"Catalog: integrity check failed → REBUILT {report.rebuilt_count} documents "
            f"from the snapshot.\n  Old DB saved to {report.backup_path}"
        )
    if report.healthy:
        snap = f"snapshot present ({report.snapshot_count})" if report.snapshot_present else "no snapshot"
        return f"Catalog: integrity OK · {report.db_count or 0} documents · {snap}. ✓"
    if report.recoverable:
        return (
            "Catalog: integrity check FAILED — recoverable.\n"
            f"  Run `procrafiler verify-catalog --rebuild` to rebuild "
            f"{report.snapshot_count} documents from the snapshot."
        )
    return (
        "Catalog: integrity check FAILED and no usable snapshot — UNRECOVERABLE here.\n"
        "  Restore from a mirror (`restore --from <mirror>`) or an offline backup."
    )
