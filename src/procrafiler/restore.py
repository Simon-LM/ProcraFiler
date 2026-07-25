"""Mirror replication & restore (durability Phase 1, see docs/durability.md).

`replicate_catalog_to_mirror` writes the catalog snapshot into the mirror's
`.procrafiler/` folder, turning the mirror into a **self-contained, restartable
unit** (its files + its catalog). `restore_from_mirror` rebuilds the library and
catalog from such a mirror after a loss (e.g. the primary partition died),
re-rooting document paths to the configured library location.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
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


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class RestorePlan:
    """What a restore WOULD do, computed before touching anything.

    `overwrites` is the dangerous set: a document already in the library whose
    content DIFFERS from the source's. Restoring used to clobber those silently —
    a user checking "does restore work?" against an old mirror rolled their
    library back with no prompt, no dry run, and no copy kept. `identical` is a
    no-op, and `library_only` documents are never touched by a restore (reported
    so the user knows the result is a merge, not a replacement).
    """

    new_files: list[str] = field(default_factory=list)
    overwrites: list[str] = field(default_factory=list)
    identical: list[str] = field(default_factory=list)
    library_only: list[str] = field(default_factory=list)

    @property
    def destructive(self) -> bool:
        return bool(self.overwrites)


def plan_restore(paths: RuntimePaths, mirror_dir: Path) -> RestorePlan:
    """Compare the source against the current library, without mutating anything."""
    plan = RestorePlan()
    source_rels: set[str] = set()
    for src in sorted(mirror_dir.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(mirror_dir)
        if rel.parts and rel.parts[0] == _META_DIR:
            continue
        rel_str = str(rel)
        source_rels.add(rel_str)
        dst = paths.library_root / rel
        if not dst.exists():
            plan.new_files.append(rel_str)
        elif _sha256(dst) == _sha256(src):
            plan.identical.append(rel_str)
        else:
            plan.overwrites.append(rel_str)

    if paths.library_root.exists():
        for existing in sorted(paths.library_root.rglob("*")):
            if existing.is_file() and not existing.name.startswith("."):
                rel_str = str(existing.relative_to(paths.library_root))
                if rel_str not in source_rels:
                    plan.library_only.append(rel_str)
    return plan


def format_plan(plan: RestorePlan, *, source: Path, library_root: Path) -> str:
    lines = [
        f"Restore plan — from {source}",
        f"                into {library_root}",
        "",
        f"  • {len(plan.new_files)} document(s) would be created",
        f"  • {len(plan.identical)} already identical (nothing to do)",
        f"  • {len(plan.overwrites)} would be OVERWRITTEN with a different version",
    ]
    if plan.overwrites:
        lines.append("")
        lines.append("  These documents differ and would be replaced:")
        for rel in plan.overwrites[:20]:
            lines.append(f"    ! {rel}")
        if len(plan.overwrites) > 20:
            lines.append(f"    … and {len(plan.overwrites) - 20} more")
        lines.append("")
        lines.append("  Each one is moved to the library trash first, so it stays recoverable.")
    if plan.library_only:
        lines.append("")
        lines.append(
            f"  • {len(plan.library_only)} document(s) exist only in your library and are "
            "left untouched (a restore merges, it does not replace the library)."
        )
    return "\n".join(lines)


@dataclass
class RestoreReport:
    files_copied: int = 0
    documents_restored: int = 0
    library_root: str = ""
    source: str = ""
    catalog_backup: str | None = None
    overwritten_to_trash: int = 0
    plan: RestorePlan | None = None
    dry_run: bool = False


def restore_from_mirror(
    paths: RuntimePaths,
    mirror_dir: Path,
    *,
    now_utc: str | None = None,
    dry_run: bool = False,
) -> RestoreReport:
    """Rebuild the library + catalog from a self-contained mirror. Raises
    FileNotFoundError if the mirror has no replicated catalog.

    A document already in the library whose content differs is NOT destroyed: it is
    moved to `Library_Trash_Manual` (preserving its relative path) before the
    restored version lands. `restore` is a recovery command, but pointing it at a
    stale mirror used to silently roll the library back — and the app's own rule is
    that it never deletes, it moves to a trash the user empties. With `dry_run` the
    plan is computed and nothing is touched at all.
    """
    snapshot_file = mirror_snapshot_path(mirror_dir)
    if not snapshot_file.is_file():
        raise FileNotFoundError(
            f"{mirror_dir} is not a restorable mirror (missing {_META_DIR}/{_SNAPSHOT_NAME}). "
            "Run `procrafiler scrub` on the source first to replicate its catalog."
        )
    data = json.loads(snapshot_file.read_text(encoding="utf-8"))
    docs = data.get("documents") or []
    old_root = Path(str(data.get("meta", {}).get("library_root", "")))

    plan = plan_restore(paths, mirror_dir)
    if dry_run:
        return RestoreReport(
            files_copied=0,
            documents_restored=len(docs),
            library_root=str(paths.library_root),
            source=str(mirror_dir),
            plan=plan,
            dry_run=True,
        )

    # 1. Copy the documents (everything except the .procrafiler metadata) into the
    #    library, preserving any differing version we are about to replace.
    files_copied = 0
    overwritten = 0
    for src in sorted(mirror_dir.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(mirror_dir)
        if rel.parts and rel.parts[0] == _META_DIR:
            continue
        dst = paths.library_root / rel
        if str(rel) in set(plan.overwrites):
            trash_target = paths.library_trash_manual_dir / rel
            trash_target.parent.mkdir(parents=True, exist_ok=True)
            if trash_target.exists():  # keep an earlier rescue rather than clobber it
                trash_target = trash_target.with_name(
                    f"{trash_target.stem}__replaced_{overwritten}{trash_target.suffix}"
                )
            shutil.move(str(dst), str(trash_target))
            overwritten += 1
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
        overwritten_to_trash=overwritten,
        plan=plan,
    )


def format_report(report: RestoreReport) -> str:
    if report.dry_run and report.plan is not None:
        return (
            format_plan(report.plan, source=Path(report.source), library_root=Path(report.library_root))
            + "\n\nDry run — nothing was changed. Re-run without --dry-run to apply."
        )
    lines = [
        f"Restored from {report.source}:",
        f"  • {report.files_copied} file(s) copied into {report.library_root}",
        f"  • {report.documents_restored} document(s) in the catalog",
    ]
    if report.overwritten_to_trash:
        lines.append(
            f"  • {report.overwritten_to_trash} pre-existing document(s) differed and were moved "
            "to the library trash (recoverable, not deleted)"
        )
    if report.plan is not None and report.plan.library_only:
        lines.append(
            f"  • {len(report.plan.library_only)} document(s) present only in your library were "
            "left untouched"
        )
    if report.catalog_backup:
        lines.append(f"  • previous catalog kept at {report.catalog_backup}")
    return "\n".join(lines)
