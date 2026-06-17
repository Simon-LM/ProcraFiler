"""Rescan — the pure-secretary sync between the LIBRARY ON DISK and the catalog.

When the user reorganizes the library BY HAND (renames a file, moves a file, or
renames/moves a whole folder), the catalog's stored paths go stale. Rescan
follows that reorganization WITHOUT any AI: it never re-reads, re-classifies or
re-names — the user's location and name always win. It only records reality.

Matching is PATH-FIRST so a still file is never hashed:
- a file already known by its path → untouched (no hash);
- a file at an UNKNOWN path is hashed once, and its sha256 decides:
    * matches a live row whose old path vanished  -> MOVED (repoint, zero AI);
    * matches a live row still present elsewhere   -> DUPLICATE (deliberate copy);
    * matches a row previously marked DELETED      -> RE-ADD (revive, zero AI);
    * matches nothing                              -> NEW (read in full + timestamped).
- a live row whose path is gone and whose content reappeared nowhere -> DELETED.

So hashing scales with the NUMBER OF CHANGED files, never the library size. This
module is pure (no DB, no I/O policy); the pipeline wires it to the catalog,
action-log and snapshot. In-place CONTENT edits (same name, new bytes) are out of
scope by design — only add / move / rename are followed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Catalog status for a row whose file the user deleted by hand. The row is KEPT
# (the fiche stays, the history is rich, and a re-deposit of the same content is
# recognised) — it is simply no longer considered present on disk.
DELETED_STATUS = "DELETED"

Row = dict[str, Any]


@dataclass
class RescanPlan:
    moved: list[tuple[Row, Path]] = field(default_factory=list)        # (catalog row, new path)
    readded: list[tuple[Row, Path]] = field(default_factory=list)      # (revived DELETED row, new path)
    duplicates: list[tuple[Path, Row]] = field(default_factory=list)   # (disk copy, original row)
    deleted: list[Row] = field(default_factory=list)                   # rows now missing from disk
    new_files: list[Path] = field(default_factory=list)               # unknown content (Phase 2 ingests)

    @property
    def is_empty(self) -> bool:
        return not (self.moved or self.readded or self.duplicates or self.deleted or self.new_files)


def walk_library_files(library_root: Path) -> list[Path]:
    """Every REAL DOCUMENT file under the library (sorted). Excluded, because they
    are never loose documents and renaming them would do harm:

    - symlinks (the library's internal back-references);
    - hidden files and anything under a hidden directory (`.git`, `.config`, …) —
      touching a `.git` internal would corrupt the repository;
    - any file inside a VERSION-CONTROL REPOSITORY (a directory that contains a
      `.git`): the user dropped a repo as a UNIT, not a pile of files to file —
      timestamping its working tree would break it. The whole subtree is left
      alone.

    The library's trash and the mirror live OUTSIDE ``library_root`` already."""
    if not library_root.exists():
        return []
    all_paths = list(library_root.rglob("*"))
    # A repo root is the parent of a `.git` entry (a dir, or a file for worktrees).
    repo_roots = [p.parent for p in all_paths if p.name == ".git"]
    files: list[Path] = []
    for p in all_paths:
        if not p.is_file() or p.is_symlink():
            continue
        if any(part.startswith(".") for part in p.relative_to(library_root).parts):
            continue  # hidden file, or under a hidden dir (e.g. .git internals)
        if any(p.is_relative_to(root) for root in repo_roots):
            continue  # inside a VCS repository — leave the unit untouched
        files.append(p)
    return sorted(files)


def reconcile(
    disk_files: list[Path],
    rows: list[Row],
    sha256_of: Callable[[Path], str],
) -> RescanPlan:
    """Pure reconciliation of the library against the catalog rows. ``sha256_of``
    is invoked ONLY for files at a path the catalog doesn't already know."""
    plan = RescanPlan()

    live_rows = [r for r in rows if r.get("status") != DELETED_STATUS]
    deleted_rows = [r for r in rows if r.get("status") == DELETED_STATUS]

    live_by_path = {str(r.get("current_path")): r for r in live_rows}
    disk_set = {str(p) for p in disk_files}

    # Files already known by their path are untouched (and never hashed).
    unknown_disk = [p for p in disk_files if str(p) not in live_by_path]

    gone_rows = [r for r in live_rows if str(r.get("current_path")) not in disk_set]
    gone_by_hash: dict[str, list[Row]] = defaultdict(list)
    for r in gone_rows:
        gone_by_hash[str(r.get("sha256"))].append(r)

    # A live row still sitting at its path → its content is "present": a copy of
    # it appearing elsewhere is a duplicate, not a move.
    present_by_hash: dict[str, Row] = {}
    for r in live_rows:
        if str(r.get("current_path")) in disk_set:
            present_by_hash.setdefault(str(r.get("sha256")), r)

    deleted_by_hash: dict[str, list[Row]] = defaultdict(list)
    for r in deleted_rows:
        deleted_by_hash[str(r.get("sha256"))].append(r)

    claimed_gone: set[int] = set()
    moved_by_hash: dict[str, Row] = {}  # the row matched as a move, per content
    for path in unknown_disk:
        digest = sha256_of(path)
        moved_candidates = gone_by_hash.get(digest)
        if moved_candidates:
            row = moved_candidates.pop(0)
            claimed_gone.add(id(row))
            moved_by_hash[digest] = row
            plan.moved.append((row, path))
            continue
        # Content already in the catalog but this file is NOT the one chosen as
        # the move → it's a duplicate, never a re-ingestion. Covers the edge case
        # where the user renames a file IN PLACE *and* drops a copy of it
        # elsewhere: the catalogued path is gone, one copy is taken as the move,
        # the other(s) are duplicates of it — not brand-new files to timestamp.
        original = present_by_hash.get(digest) or moved_by_hash.get(digest)
        if original is not None:
            plan.duplicates.append((path, original))
            continue
        readd_candidates = deleted_by_hash.get(digest)
        if readd_candidates:
            plan.readded.append((readd_candidates.pop(0), path))
            continue
        plan.new_files.append(path)

    plan.deleted = [r for r in gone_rows if id(r) not in claimed_gone]
    return plan
