"""Put a whole run back the way it was.

Until now a `process-all` was, in practice, irreversible: the only recourse was
to move every document back by hand and then `rescan`. That is the single thing
that makes a first run on real documents frightening — not that the AI might be
wrong, but that being wrong would cost an evening of manual repair.

**What "undo" means here, precisely.** Every event of one run carries the same
`run_id`. Undoing it returns each document this run filed to the exact Inbox
subfolder it was dropped in, so the next run sees the same sets it saw before —
files dropped together stay together.

**It refuses rather than guesses.** A document is restored only when it is still
*exactly* where the run left it, which is checked against the catalog. If you have
since renamed it, moved it by hand, or `rescan` has repointed it, that document is
reported as blocked and left strictly alone. An undo that "did its best" on a
library the user has since reorganised would be worse than no undo at all.

**Nothing is deleted.** The document goes back to the Inbox; its mirror copy is
moved to `Mirror_Trash` (where the normal retention eventually reclaims it), and
its catalog row is purged because the document is no longer in the library. The
only thing actually removed is the hidden text sidecar — derived data, a cache of
what the AI read, regenerable at the cost of one call. That is counted and
reported rather than done quietly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from shutil import move
from uuid import uuid4

from procrafiler.catalog import CatalogRepository  # type: ignore[reportMissingImports]
from procrafiler.config import RuntimePaths  # type: ignore[reportMissingImports]

# Events whose `path_after` is where the run put a document. The last one seen for
# a given operation wins: a file filed and then regrouped moved twice.
_PLACEMENT_ACTIONS = {
    "move_to_library",
    "organize_placed",
    "library_file_regrouped",
}
# Where a duplicate was set aside. Restoring it means bringing it back too — the
# user undoing a run expects their Inbox as it was, duplicates included.
_TRASH_ACTIONS = {"move_to_inbox_trash_manual"}


@dataclass
class UndoItem:
    operation_id: str
    current_path: Path
    original_path: Path


@dataclass
class UndoPlan:
    run_id: str = ""
    started_utc: str = ""
    restore: list[UndoItem] = field(default_factory=list)
    symlinks: list[Path] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.restore and not self.symlinks


@dataclass
class UndoReport:
    run_id: str = ""
    restored: int = 0
    mirrors_quarantined: int = 0
    sidecars_dropped: int = 0
    catalog_rows_purged: int = 0
    symlinks_removed: int = 0
    blocked: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _read_events(paths: RuntimePaths) -> list[dict]:
    log_file = paths.actions_log_file
    if not log_file.is_file():
        return []
    events: list[dict] = []
    try:
        with log_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # a truncated tail must not hide the rest
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


def latest_run_id(paths: RuntimePaths) -> str | None:
    """The most recent run that wrote a `run_id`. None on a log with none —
    runs recorded before this existed cannot be identified, and saying so is
    better than undoing an arbitrary slice of history."""
    for event in reversed(_read_events(paths)):
        run_id = event.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return None


def list_runs(paths: RuntimePaths, *, limit: int = 10) -> list[tuple[str, str, int]]:
    """Recent runs as (run_id, first event time, number of documents filed)."""
    seen: dict[str, tuple[str, int]] = {}
    for event in _read_events(paths):
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        when, filed = seen.get(run_id, (str(event.get("event_time_utc") or ""), 0))
        if event.get("action") == "move_to_library":
            filed += 1
        seen[run_id] = (when, filed)
    rows = [(run_id, when, filed) for run_id, (when, filed) in seen.items()]
    return rows[-limit:][::-1]


def plan_undo(paths: RuntimePaths, run_id: str) -> UndoPlan:
    """What undoing `run_id` would do, computed without touching anything."""
    plan = UndoPlan(run_id=run_id)
    events = [e for e in _read_events(paths) if e.get("run_id") == run_id]
    if not events:
        plan.blocked.append(f"no events found for run {run_id}")
        return plan
    plan.started_utc = str(events[0].get("event_time_utc") or "")

    origins: dict[str, str] = {}   # operation_id → the Inbox path it came from
    placements: dict[str, str] = {}  # operation_id → where it ended up
    for event in events:
        operation_id = str(event.get("operation_id") or "")
        action = event.get("action")
        if not operation_id:
            continue
        if action == "move_to_queue" and event.get("path_before"):
            origins[operation_id] = str(event["path_before"])
        elif action in _PLACEMENT_ACTIONS and event.get("path_after"):
            placements[operation_id] = str(event["path_after"])
        elif action in _TRASH_ACTIONS and event.get("path_after"):
            placements[operation_id] = str(event["path_after"])
        elif action == "symlink_left" and event.get("path_after"):
            plan.symlinks.append(Path(str(event["path_after"])))

    catalog = CatalogRepository(paths.catalog_db_file)
    for operation_id, current in placements.items():
        origin = origins.get(operation_id)
        current_path = Path(current)
        if not origin:
            plan.blocked.append(f"{current_path.name}: no record of where it came from")
            continue
        original_path = Path(origin)
        try:  # never restore outside the Inbox, whatever the log says
            original_path.relative_to(paths.inbox_dir)
        except ValueError:
            plan.blocked.append(f"{current_path.name}: its recorded origin is outside the Inbox")
            continue
        if not current_path.is_file():
            plan.blocked.append(f"{current_path.name}: no longer at the path the run left it")
            continue
        # The catalog is the authority on where a document is now. A document it
        # does not know at this path has been moved or renamed since the run, and
        # must not be dragged back on the strength of a stale log line.
        inside_library = _is_inside(current_path, paths.library_root)
        if inside_library and catalog.find_by_current_path(str(current_path)) is None:
            plan.blocked.append(f"{current_path.name}: moved or renamed since the run")
            continue
        plan.restore.append(UndoItem(operation_id, current_path, original_path))

    plan.restore.sort(key=lambda item: str(item.current_path))
    return plan


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def format_undo_plan(plan: UndoPlan) -> str:
    lines = [f"Undo plan — run {plan.run_id}"]
    if plan.started_utc:
        lines.append(f"             started {plan.started_utc}")
    lines.append("")
    lines.append(f"  • {len(plan.restore)} document(s) would go back to the Inbox")
    if plan.symlinks:
        lines.append(f"  • {len(plan.symlinks)} shortcut(s) left by the run would be removed")
    for item in plan.restore[:20]:
        lines.append(f"    ← {item.original_path.name}")
    if len(plan.restore) > 20:
        lines.append(f"    … and {len(plan.restore) - 20} more")
    if plan.blocked:
        lines.append("")
        lines.append(f"  ! {len(plan.blocked)} left untouched — changed since the run:")
        for reason in plan.blocked[:20]:
            lines.append(f"    ! {reason}")
        if len(plan.blocked) > 20:
            lines.append(f"    … and {len(plan.blocked) - 20} more")
    lines.append("")
    lines.append("  Documents return to the exact Inbox subfolder they were dropped in.")
    lines.append("  Mirror copies go to Mirror_Trash; nothing is deleted.")
    return "\n".join(lines)


def apply_undo(
    paths: RuntimePaths,
    plan: UndoPlan,
    *,
    now_utc: str | None = None,
    log_event=None,
) -> UndoReport:
    """Perform `plan`. Each document is one independent move, so an interruption
    leaves the rest for a second `undo-run` rather than a half-state."""
    report = UndoReport(run_id=plan.run_id, blocked=list(plan.blocked))
    catalog = CatalogRepository(paths.catalog_db_file)

    for item in plan.restore:
        try:
            row = catalog.find_by_current_path(str(item.current_path))
            target = _unique(item.original_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            move(str(item.current_path), str(target))
            report.restored += 1

            report.sidecars_dropped += _drop_sidecar(item.current_path)
            report.mirrors_quarantined += _quarantine_mirror(paths, item.current_path)
            if row is not None and row.get("doc_id"):
                catalog.purge_document(str(row["doc_id"]))
                report.catalog_rows_purged += 1
            if log_event is not None:
                log_event(
                    operation_id=str(uuid4()),
                    action="run_undone_file",
                    status="success",
                    message="Document returned to the Inbox by undo-run",
                    path_before=str(item.current_path),
                    path_after=str(target),
                )
        except OSError as exc:
            report.failures.append(f"{item.current_path.name}: {exc}")

    for link in plan.symlinks:
        try:
            if link.is_symlink():
                link.unlink()
                report.symlinks_removed += 1
        except OSError as exc:  # pragma: no cover - a shortcut is never critical
            report.failures.append(f"{link.name}: {exc}")

    return report


def _unique(target: Path) -> Path:
    """Never overwrite something already sitting at the Inbox path."""
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    index = 1
    while True:
        candidate = parent / f"{stem}__{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _drop_sidecar(document: Path) -> int:
    """Remove the hidden text sidecar. It is a cache of what the AI read, and it
    must NOT follow the document into the Inbox: the Inbox scan lists every file,
    so a sidecar there would be ingested as a document of its own."""
    sidecar = document.parent / f".{document.name}.txt"
    try:
        if sidecar.is_file():
            sidecar.unlink()
            return 1
    except OSError:
        pass
    return 0


def _quarantine_mirror(paths: RuntimePaths, library_path: Path) -> int:
    """Move the mirror copy to `Mirror_Trash` rather than deleting it — the same
    never-delete rule the rest of the app follows, and the existing retention
    reclaims it later."""
    if not _is_inside(library_path, paths.library_root):
        return 0
    try:
        relative = library_path.relative_to(paths.library_root)
    except ValueError:  # pragma: no cover - guarded just above
        return 0
    mirror_copy = paths.mirror_root / relative
    if not mirror_copy.is_file():
        return 0
    target = _unique(paths.mirror_trash_dir / relative)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        move(str(mirror_copy), str(target))
        return 1
    except OSError:
        return 0


def format_undo_report(report: UndoReport) -> str:
    lines = [
        f"Undo of run {report.run_id}:",
        f"  {report.restored} document(s) returned to the Inbox",
    ]
    if report.mirrors_quarantined:
        lines.append(f"  {report.mirrors_quarantined} mirror copy(ies) moved to Mirror_Trash")
    if report.catalog_rows_purged:
        lines.append(f"  {report.catalog_rows_purged} catalog entry(ies) removed")
    if report.sidecars_dropped:
        lines.append(
            f"  {report.sidecars_dropped} cached text sidecar(s) dropped "
            "(they will be re-read, at the cost of an AI call, if you process these again)"
        )
    if report.symlinks_removed:
        lines.append(f"  {report.symlinks_removed} shortcut(s) removed")
    if report.blocked:
        lines.append(f"  {len(report.blocked)} left untouched because they changed since the run")
    if report.failures:
        lines.append(f"  {len(report.failures)} could not be moved:")
        lines.extend(f"    ! {failure}" for failure in report.failures)
    return "\n".join(lines)
