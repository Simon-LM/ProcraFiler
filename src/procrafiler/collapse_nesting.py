"""Repair tool: collapse accidental DOUBLE-NESTING in the library.

Occasionally a grouping step files a document into a child folder whose name
repeats its parent — e.g. ``Education/OpenClassrooms/OpenClassrooms/2025/…`` —
splitting what should be one folder across two levels. This tool finds those and
merges the inner folder back up into its parent.

SAFETY — it ONLY touches a directory whose name EQUALS its immediate parent's
name (``…/X/X`` -> ``…/X``). Two folders that merely SHARE a name in DIFFERENT
places (a ``Divers`` under Personal AND a ``Divers`` under Work, several
``Factures`` folders…) are perfectly normal and are NEVER matched: the rule is
purely "consecutive identical segments on the same path". Existing files are
never overwritten — a name collision is reported, not resolved by clobbering.

Pure logic (no CLI, no I/O policy) so it is fully testable; the CLI wires it to
the library root with a dry-run default.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CollapseReport:
    redundant_dirs: list[Path] = field(default_factory=list)  # the inner …/X/X dirs
    moves: list[tuple[Path, Path]] = field(default_factory=list)  # (src, dst) files moved/planned
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)  # dst already exists as a file


def find_double_nestings(root: Path) -> list[Path]:
    """Return every directory whose name equals its immediate parent's name,
    deepest first (so ``X/X/X`` collapses from the inside out)."""
    matches = [
        d
        for d in root.rglob("*")
        if d.is_dir() and d.parent != root.parent and d.name == d.parent.name
    ]
    matches.sort(key=lambda p: len(p.parts), reverse=True)
    return matches


def _merge_move(src: Path, dst: Path, report: CollapseReport) -> None:
    """Move ``src`` to ``dst``. If ``dst`` is an existing directory, merge into
    it recursively; if it's an existing file, record a conflict and leave both."""
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        report.moves.append((src, dst))
        return
    if src.is_dir() and dst.is_dir():
        for item in list(src.iterdir()):
            _merge_move(item, dst / item.name, report)
        try:
            src.rmdir()
        except OSError:
            pass
        return
    report.conflicts.append((src, dst))


def collapse_double_nestings(root: Path, *, apply: bool) -> CollapseReport:
    """Find (and, when ``apply``, perform) the collapse of every ``…/X/X`` into
    ``…/X`` under ``root``. Dry-run by default: the report lists what WOULD move."""
    report = CollapseReport()
    report.redundant_dirs = find_double_nestings(root)
    for child in report.redundant_dirs:
        parent = child.parent
        if apply:
            for item in list(child.iterdir()):
                _merge_move(item, parent / item.name, report)
            try:
                child.rmdir()
            except OSError:
                pass
        else:
            for item in sorted(child.rglob("*")):
                if item.is_file():
                    report.moves.append((item, parent / item.relative_to(child)))
    return report
