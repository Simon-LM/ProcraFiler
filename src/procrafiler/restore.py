"""Mirror replication & restore (durability Phase 1, see docs/durability.md).

`replicate_catalog_to_mirror` writes the catalog snapshot into the mirror's
`.procrafiler/` folder, turning the mirror into a **self-contained, restartable
unit** (its files + its catalog). `restore_from_mirror` rebuilds the library and
catalog from such a mirror after a loss (e.g. the primary partition died),
re-rooting document paths to the configured library location.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.catalog_verify import rebuild_catalog_from_snapshot
from procrafiler.config import RuntimePaths, load_feature_settings
from procrafiler.pipeline import _write_catalog_snapshot

_META_DIR = ".procrafiler"
_SNAPSHOT_NAME = "catalog_snapshot.json"


def mirror_snapshot_path(mirror_root: Path) -> Path:
    return mirror_root / _META_DIR / _SNAPSHOT_NAME


def replicate_catalog_to_mirror(paths: RuntimePaths) -> bool:
    """Write a fresh catalog snapshot into the mirror's `.procrafiler/` folder so
    the mirror is self-contained (restorable). No-op if the mirror is disabled or
    not present. Returns True when written."""
    features = load_feature_settings(paths)["features"]
    if not features.get("mirror_sync", True) or not paths.mirror_root.exists():
        return False
    repo = CatalogRepository(paths.catalog_db_file)
    _write_catalog_snapshot(paths, repo, target=mirror_snapshot_path(paths.mirror_root))
    return True


@dataclass
class RestoreReport:
    files_copied: int = 0
    documents_restored: int = 0
    library_root: str = ""
    source: str = ""
    catalog_backup: str | None = None


def restore_from_mirror(
    paths: RuntimePaths, mirror_dir: Path, *, now_utc: str | None = None
) -> RestoreReport:
    """Rebuild the library + catalog from a self-contained mirror. Raises
    FileNotFoundError if the mirror has no replicated catalog."""
    snapshot_file = mirror_snapshot_path(mirror_dir)
    if not snapshot_file.is_file():
        raise FileNotFoundError(
            f"{mirror_dir} is not a restorable mirror (missing {_META_DIR}/{_SNAPSHOT_NAME}). "
            "Run `procrafiler scrub` on the source first to replicate its catalog."
        )
    data = json.loads(snapshot_file.read_text(encoding="utf-8"))
    docs = data.get("documents") or []
    old_root = Path(str(data.get("meta", {}).get("library_root", "")))

    # 1. Copy the documents (everything except the .procrafiler metadata) into the library.
    files_copied = 0
    for src in sorted(mirror_dir.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(mirror_dir)
        if rel.parts and rel.parts[0] == _META_DIR:
            continue
        dst = paths.library_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        files_copied += 1

    # 2. Rebuild the catalog, re-rooting paths to the new library location.
    reroot = (old_root, paths.library_root) if str(old_root) else None
    count, backup = rebuild_catalog_from_snapshot(paths, docs, now_utc=now_utc, reroot=reroot)

    # Only surface a backup when the replaced DB actually held data (restoring into a
    # fresh location leaves an empty 0-byte DB that isn't worth mentioning).
    backup_path = Path(backup)
    kept = str(backup_path) if backup_path.exists() and backup_path.stat().st_size > 0 else None

    return RestoreReport(
        files_copied=files_copied,
        documents_restored=count,
        library_root=str(paths.library_root),
        source=str(mirror_dir),
        catalog_backup=kept,
    )


def format_report(report: RestoreReport) -> str:
    lines = [
        f"Restored from {report.source}:",
        f"  • {report.files_copied} file(s) copied into {report.library_root}",
        f"  • {report.documents_restored} document(s) in the catalog",
    ]
    if report.catalog_backup:
        lines.append(f"  • previous catalog kept at {report.catalog_backup}")
    return "\n".join(lines)
