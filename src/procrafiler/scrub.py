"""Integrity scrub (durability Phase 1, see docs/durability.md).

Re-hash stored documents and compare to the catalog `sha256`, on the **library**
and (when enabled) the **mirror**. This is **detection only** — repairing a bad
copy from a good one (`heal`) is the next step. The catalog hash is the source of
truth, so a scrub finds both silent corruption (bit rot) and tampering.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import RuntimePaths, load_feature_settings

_OK = "ok"
_CORRUPT = "corrupt"
_MISSING = "missing"


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


@dataclass
class ScrubIssue:
    doc_id: str
    relative_path: str
    where: str  # "library" | "mirror"
    state: str  # "corrupt" | "missing"


@dataclass
class ScrubReport:
    checked: int = 0
    library_ok: int = 0
    mirror_checked: int = 0
    mirror_ok: int = 0
    mirror_enabled: bool = True
    issues: list[ScrubIssue] = field(default_factory=list)

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
    now_utc: str | None = None,
) -> ScrubReport:
    """Verify up to `limit` stored documents (least-recently-verified first;
    `limit=None` = all). Documents whose **library** copy matches are marked
    verified; mismatches/missing on either copy are collected as issues."""
    when = now_utc or datetime.now(timezone.utc).isoformat()
    mirror_enabled = bool(load_feature_settings(paths)["features"].get("mirror_sync", True))
    do_mirror = check_mirror and mirror_enabled

    report = ScrubReport(mirror_enabled=mirror_enabled)
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

        if _verify(lib_path, expected) == _OK:
            report.library_ok += 1
            verified.append(doc_id)  # only mark verified when the library copy is good
        else:
            state = _MISSING if not lib_path.is_file() else _CORRUPT
            report.issues.append(ScrubIssue(doc_id, rel_str, "library", state))

        if do_mirror and rel is not None:
            mirror_path = paths.mirror_root / rel
            report.mirror_checked += 1
            mir_state = _verify(mirror_path, expected)
            if mir_state == _OK:
                report.mirror_ok += 1
            else:
                report.issues.append(ScrubIssue(doc_id, rel_str, "mirror", mir_state))

    catalog.mark_verified(verified, when_utc=when)
    return report


def format_report(report: ScrubReport) -> str:
    lines = [
        f"Scrub: {report.checked} document(s) checked — "
        f"library {report.library_ok}/{report.checked} OK"
    ]
    if report.mirror_enabled:
        lines[0] += f", mirror {report.mirror_ok}/{report.mirror_checked} OK"
    else:
        lines[0] += ", mirror disabled"
    if report.healthy:
        lines.append("All verified copies match the catalog. ✓")
    else:
        lines.append(f"PROBLEMS: {report.corrupt} corrupt, {report.missing} missing")
        for issue in report.issues:
            lines.append(f"  [{issue.state}] {issue.where}: {issue.relative_path}")
    return "\n".join(lines)
