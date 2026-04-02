from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copy2, move

from procrafiler.config import RuntimePaths


@dataclass(frozen=True)
class MirrorSyncResult:
    success: bool
    mirror_target: Path
    quarantined_path: Path | None = None
    error: str | None = None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem}__{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def sync_library_file_to_mirror(
    paths: RuntimePaths,
    library_file: Path,
    *,
    now_utc: datetime | None = None,
) -> MirrorSyncResult:
    if not library_file.exists() or not library_file.is_file():
        return MirrorSyncResult(success=False, mirror_target=paths.mirror_root, error="source_missing")

    try:
        relative_path = library_file.relative_to(paths.library_root)
    except ValueError:
        return MirrorSyncResult(success=False, mirror_target=paths.mirror_root, error="outside_library_root")

    mirror_target = paths.mirror_root / relative_path
    mirror_target.parent.mkdir(parents=True, exist_ok=True)

    quarantined_path: Path | None = None
    if mirror_target.exists():
        src_hash = _file_sha256(library_file)
        dst_hash = _file_sha256(mirror_target)
        if src_hash != dst_hash:
            dt = now_utc or datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            stamp = dt.strftime("%Y-%m-%d_%H-%M-%S")
            quarantine_candidate = (
                paths.mirror_trash_dir
                / relative_path.parent
                / f"{mirror_target.stem}__quarantined_{stamp}{mirror_target.suffix}"
            )
            quarantined_path = _ensure_unique_path(quarantine_candidate)
            quarantined_path.parent.mkdir(parents=True, exist_ok=True)
            move(str(mirror_target), str(quarantined_path))

    try:
        copy2(str(library_file), str(mirror_target))
        src_hash = _file_sha256(library_file)
        dst_hash = _file_sha256(mirror_target)
        if src_hash != dst_hash:
            return MirrorSyncResult(
                success=False,
                mirror_target=mirror_target,
                quarantined_path=quarantined_path,
                error="hash_mismatch",
            )
        return MirrorSyncResult(success=True, mirror_target=mirror_target, quarantined_path=quarantined_path)
    except Exception as exc:  # noqa: BLE001
        return MirrorSyncResult(
            success=False,
            mirror_target=mirror_target,
            quarantined_path=quarantined_path,
            error=str(exc),
        )


def purge_mirror_trash(
    paths: RuntimePaths,
    *,
    retention_days: int,
    now_utc: datetime | None = None,
) -> int:
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    removed = 0
    for file_path in sorted([p for p in paths.mirror_trash_dir.rglob("*") if p.is_file()]):
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            file_path.unlink(missing_ok=True)
            removed += 1

    for directory in sorted([p for p in paths.mirror_trash_dir.rglob("*") if p.is_dir()], reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()

    return removed
