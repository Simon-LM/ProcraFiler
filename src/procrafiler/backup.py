"""Cold backup (durability Phase 1, see docs/durability.md).

`backup --to <dir>` writes a **consistent, self-contained, dated** archive of the
library + catalog — a mirror-shaped tarball (library files at their relative paths
plus `.procrafiler/catalog_snapshot.json`) with a `.sha256` checksum. It is
**immutable**: each run is a new dated archive; keep N, prune the oldest.
`restore --from-archive` extracts it and reuses the mirror-restore path.

Encryption of the bundle (for cloud/offsite) is a follow-up — for now, place the
archive on **offline / air-gapped** media. The last-backup date is recorded so
`status` can remind you when a fresh backup is overdue.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import RuntimePaths
from procrafiler.pipeline import _write_catalog_snapshot
from procrafiler.restore import _META_DIR, _SNAPSHOT_NAME, RestoreReport, restore_from_mirror

_LAST_BACKUP_FILE = "last_backup.txt"
_REMIND_AFTER_DAYS = 90


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BackupReport:
    archive: str
    files: int
    documents: int
    sha256: str
    size: int


def create_backup(paths: RuntimePaths, out_dir: Path, *, now_utc: str | None = None) -> BackupReport:
    """Write a dated, self-contained `.tar.gz` of the library + catalog snapshot
    into `out_dir`, plus a `.sha256`. Records the backup date."""
    when = now_utc or datetime.now(timezone.utc).isoformat()
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = when[:19].replace(":", "").replace("-", "")  # YYYYMMDDTHHMMSS
    archive = out_dir / f"procrafiler-backup-{stamp}.tar.gz"

    files = 0
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "snapshot.json"
        _write_catalog_snapshot(paths, CatalogRepository(paths.catalog_db_file), target=snap)
        documents = _snapshot_document_count(snap)

        tmp_archive = Path(td) / "out.tar.gz"
        with tarfile.open(tmp_archive, "w:gz") as tar:
            if paths.library_root.exists():
                for f in sorted(paths.library_root.rglob("*")):
                    if f.is_file():
                        tar.add(f, arcname=str(f.relative_to(paths.library_root)))
                        files += 1
            tar.add(snap, arcname=f"{_META_DIR}/{_SNAPSHOT_NAME}")

        digest = _sha256_file(tmp_archive)
        shutil.move(str(tmp_archive), str(archive))

    archive.with_name(archive.name + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    record_backup(paths, when)
    return BackupReport(
        archive=str(archive), files=files, documents=documents,
        sha256=digest, size=archive.stat().st_size,
    )


def _snapshot_document_count(snapshot_file: Path) -> int:
    try:
        data = json.loads(snapshot_file.read_text(encoding="utf-8"))
        return len(data.get("documents") or [])
    except (OSError, ValueError):
        return 0


def restore_from_archive(paths: RuntimePaths, archive: Path, *, now_utc: str | None = None) -> RestoreReport:
    """Extract a backup archive and rebuild the library + catalog from it (reuses
    the mirror-restore path, since the archive is mirror-shaped)."""
    archive = Path(archive).expanduser()
    if not archive.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive}")
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(archive, "r:*") as tar:
            try:
                tar.extractall(td, filter="data")  # Python 3.12+: refuse unsafe members
            except TypeError:
                tar.extractall(td)  # Python < 3.12
        report = restore_from_mirror(paths, Path(td), now_utc=now_utc)
    report.source = str(archive)  # show the archive, not the ephemeral temp dir
    return report


def record_backup(paths: RuntimePaths, when_iso: str) -> None:
    paths.state_root.mkdir(parents=True, exist_ok=True)
    (paths.state_root / _LAST_BACKUP_FILE).write_text(when_iso + "\n", encoding="utf-8")


def last_backup_utc(paths: RuntimePaths) -> str | None:
    f = paths.state_root / _LAST_BACKUP_FILE
    if not f.is_file():
        return None
    return f.read_text(encoding="utf-8").strip() or None


def backup_reminder(paths: RuntimePaths, *, now_utc: str | None = None) -> str | None:
    """A nudge string when a backup is overdue or never made, else None."""
    last = last_backup_utc(paths)
    if last is None:
        return "No offline backup yet — consider: procrafiler backup --to <dir>"
    try:
        last_dt = datetime.fromisoformat(last)
        now_dt = datetime.fromisoformat(now_utc) if now_utc else datetime.now(timezone.utc)
    except ValueError:
        return None
    days = (now_dt - last_dt).days
    if days >= _REMIND_AFTER_DAYS:
        return f"Last offline backup was {days} days ago — consider a fresh one (procrafiler backup --to <dir>)."
    return None


def format_report(report: BackupReport) -> str:
    mb = report.size / (1024 * 1024)
    return (
        f"Backup written: {report.archive}\n"
        f"  • {report.files} file(s), {report.documents} document(s), {mb:.1f} MiB\n"
        f"  • sha256 {report.sha256[:16]}… (saved alongside as .sha256)\n"
        "  Store it on offline / air-gapped media."
    )
