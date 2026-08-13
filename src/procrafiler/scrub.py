"""Integrity scrub & heal (durability Phase 1, see docs/durability.md).

Re-hash stored documents and compare to the catalog `sha256`, on the **library**
and (when enabled) the **mirror**. The catalog hash is the source of truth, so a
scrub finds both silent corruption (bit rot) and tampering.

With `repair=True` (`scrub --repair`) it also **heals**: a bad copy is restored
from a verified-good one (library ↔ mirror), atomically and re-verified. It never
restores from a source that does not itself match the catalog hash, and never
touches a document whose only copies are all bad (reported as unrecoverable).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from procrafiler.catalog import CatalogRepository
from procrafiler.config import RuntimePaths, load_feature_settings
from procrafiler.pipeline import _append_action_log

_OK = "ok"
_CORRUPT = "corrupt"
_MISSING = "missing"

# How long a document may go unverified before `status` says so.
#
# Not chosen from how often disks rot, but from **how long a good copy survives to
# repair from**. When a library file changes, the mirror does not overwrite its own
# copy: it QUARANTINES it into `Mirror_Trash` with a timestamp (see `mirror.py`), so
# the healthy version outlives the corruption — until `purge-mirror-trash` removes
# it, governed by `mirror_retention_days`, whose default is 30 days.
#
# Past that window the corrupt file may be the only version left, and `scrub
# --repair` has nothing to restore from. Hence 30: the same figure as the mirror
# retention it is bounded by, and as the backup reminder.
REMIND_AFTER_DAYS = 30


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(path: Path, expected_sha256: str) -> str:
    if not path.is_file():
        return _MISSING
    return _OK if _hash_file(path) == expected_sha256 else _CORRUPT


def _restore(src: Path, dst: Path, expected_sha256: str) -> bool:
    """Copy `src`→`dst` atomically, but ONLY if `src` matches the catalog hash and
    the written copy verifies. Returns False (and changes nothing) otherwise — we
    never restore from a copy that is not itself known-good."""
    if not src.is_file() or _hash_file(src) != expected_sha256:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".heal-tmp")
    try:
        shutil.copy2(src, tmp)
        if _hash_file(tmp) != expected_sha256:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, dst)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


@dataclass
class ScrubIssue:
    doc_id: str
    relative_path: str
    where: str  # "library" | "mirror"
    state: str  # "corrupt" | "missing"


@dataclass
class RepairAction:
    doc_id: str
    relative_path: str
    where: str  # the copy that was rewritten: "library" | "mirror"
    source: str  # the verified-good copy it was restored from: "mirror" | "library"


@dataclass
class ScrubReport:
    checked: int = 0
    library_ok: int = 0
    mirror_checked: int = 0
    mirror_ok: int = 0
    mirror_enabled: bool = True
    repair_attempted: bool = False
    issues: list[ScrubIssue] = field(default_factory=list)
    repaired: list[RepairAction] = field(default_factory=list)

    @property
    def corrupt(self) -> int:
        return sum(1 for i in self.issues if i.state == _CORRUPT)

    @property
    def missing(self) -> int:
        return sum(1 for i in self.issues if i.state == _MISSING)

    @property
    def healthy(self) -> bool:
        return not self.issues


def scrub(
    paths: RuntimePaths,
    catalog: CatalogRepository,
    *,
    limit: int | None = None,
    check_mirror: bool = True,
    repair: bool = False,
    now_utc: str | None = None,
) -> ScrubReport:
    """Verify up to `limit` stored documents (least-recently-verified first;
    `limit=None` = all). When `repair`, heal a bad copy from a verified-good one.
    A document is marked verified only when its **library** copy matches."""
    when = now_utc or datetime.now(timezone.utc).isoformat()
    features = load_feature_settings(paths)["features"]
    mirror_enabled = bool(features.get("mirror_sync", True))
    do_mirror = check_mirror and mirror_enabled
    op_id = str(uuid4())

    report = ScrubReport(mirror_enabled=mirror_enabled, repair_attempted=repair)
    verified: list[str] = []

    for doc in catalog.documents_for_scrub(limit=limit):
        doc_id = str(doc["doc_id"])
        expected = str(doc["sha256"])
        lib_path = Path(str(doc["current_path"]))
        report.checked += 1

        try:
            rel = lib_path.relative_to(paths.library_root)
            rel_str = str(rel)
        except ValueError:
            rel = None
            rel_str = str(lib_path)

        lib_state = _verify(lib_path, expected)
        mir_path = (paths.mirror_root / rel) if (do_mirror and rel is not None) else None
        mir_state = _verify(mir_path, expected) if mir_path is not None else None

        if repair:
            # Restore a bad library from a good mirror first…
            if lib_state != _OK and mir_state == _OK and _restore(mir_path, lib_path, expected):  # type: ignore[arg-type]
                lib_state = _OK
                report.repaired.append(RepairAction(doc_id, rel_str, "library", "mirror"))
                _log_repair(paths, op_id, "library", lib_path, features)
            # …then restore a bad mirror from the (now) good library.
            if mir_path is not None and mir_state != _OK and lib_state == _OK and _restore(lib_path, mir_path, expected):
                mir_state = _OK
                report.repaired.append(RepairAction(doc_id, rel_str, "mirror", "library"))
                _log_repair(paths, op_id, "mirror", mir_path, features)

        if lib_state == _OK:
            report.library_ok += 1
            verified.append(doc_id)
        else:
            report.issues.append(ScrubIssue(doc_id, rel_str, "library", lib_state))

        if mir_path is not None:
            report.mirror_checked += 1
            if mir_state == _OK:
                report.mirror_ok += 1
            else:
                report.issues.append(ScrubIssue(doc_id, rel_str, "mirror", str(mir_state)))

    catalog.mark_verified(verified, when_utc=when)
    return report


def _log_repair(paths: RuntimePaths, op_id: str, where: str, path: Path, features: dict[str, bool]) -> None:
    _append_action_log(
        paths,
        operation_id=op_id,
        action="heal_restore",
        status="success",
        message=f"restored {where} copy from the verified-good copy",
        path_after=str(path),
        features=features,
    )


def format_report(report: ScrubReport) -> str:
    head = (
        f"Scrub: {report.checked} document(s) checked — "
        f"library {report.library_ok}/{report.checked} OK"
    )
    head += (
        f", mirror {report.mirror_ok}/{report.mirror_checked} OK"
        if report.mirror_enabled
        else ", mirror disabled"
    )
    lines = [head]
    if report.repaired:
        lines.append(f"Repaired {len(report.repaired)} copies:")
        for r in report.repaired:
            lines.append(f"  [repaired] {r.where} ← {r.source}: {r.relative_path}")
    if report.healthy:
        lines.append("All verified copies match the catalog. ✓")
    else:
        label = "UNRECOVERABLE" if report.repair_attempted else "PROBLEMS"
        lines.append(f"{label}: {report.corrupt} corrupt, {report.missing} missing")
        for issue in report.issues:
            lines.append(f"  [{issue.state}] {issue.where}: {issue.relative_path}")
    return "\n".join(lines)


@dataclass
class IntegrityStatus:
    """How overdue the library's integrity check is, for `status` to report."""

    documents: int = 0
    overdue: int = 0
    oldest_days: int | None = None

    @property
    def is_overdue(self) -> bool:
        return self.overdue > 0


def integrity_status(
    catalog: CatalogRepository, *, now_utc: str | None = None
) -> IntegrityStatus:
    """Count the documents whose content has not been confirmed recently.

    Every stored document counts, verified or not: one whose sha256 was computed at
    filing and never re-checked since ages the same way as one a scrub confirmed.
    See `CatalogRepository.content_confirmed_timestamps`.
    """
    try:
        now_dt = datetime.fromisoformat(now_utc) if now_utc else datetime.now(timezone.utc)
    except ValueError:
        return IntegrityStatus()
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    status = IntegrityStatus()
    for stamp in catalog.content_confirmed_timestamps():
        status.documents += 1
        try:
            # Python parses both shapes the catalog holds (`…Z` and `…+00:00`).
            confirmed = datetime.fromisoformat(stamp)
        except ValueError:
            # An unreadable timestamp is not a reason to under-report: a document
            # whose age cannot be established has not been confirmed either.
            status.overdue += 1
            continue
        if confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=timezone.utc)
        days = (now_dt - confirmed).days
        if status.oldest_days is None or days > status.oldest_days:
            status.oldest_days = days
        if days >= REMIND_AFTER_DAYS:
            status.overdue += 1
    return status


def integrity_reminder(status: IntegrityStatus) -> str | None:
    """A nudge when documents have gone unchecked too long, else None.

    Separate from `integrity_status` so `status` can print the figures whether or
    not they warrant a warning — a number you watch is how you notice it growing.
    """
    if not status.is_overdue:
        return None
    return (
        f"{status.overdue} of {status.documents} document(s) unverified for "
        f"{REMIND_AFTER_DAYS}+ days — run: procrafiler scrub"
    )
