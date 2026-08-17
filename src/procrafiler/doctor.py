"""Diagnostic checks for ProcraFiler runtime configuration.

`procrafiler doctor` runs every check defined here and prints a structured
report. Each check produces a `DoctorCheck` (OK / WARN / FAIL / SKIP);
the CLI exits non-zero if any FAIL is present.

The checks are deliberately read-only and fast — no file is moved, no
network call is made by default. The lock check briefly tries to acquire
the runtime lock to detect a concurrent process, but releases it
immediately.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

from procrafiler.ai_naming import LOCAL_PROVIDERS, SUPPORTED_AI_TASKS, task_chain_from_env
from procrafiler.config import RuntimePaths, layout_conflicts, load_feature_settings
from procrafiler.pricing import load_price_table
from procrafiler.runtime_lock import probe_runtime_lock
from procrafiler.user_setup import on_same_disk


STATUS_OK = "OK"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"


@dataclass(frozen=True)
class DoctorCheck:
    section: str
    name: str
    status: str
    message: str


def _check_path_writable(section: str, name: str, path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck(section, name, STATUS_FAIL, f"missing: {path}")
    if not path.is_dir():
        return DoctorCheck(section, name, STATUS_FAIL, f"not a directory: {path}")
    if not os.access(path, os.W_OK):
        return DoctorCheck(section, name, STATUS_FAIL, f"not writable: {path}")
    return DoctorCheck(section, name, STATUS_OK, str(path))


def check_paths(paths: RuntimePaths, *, mirror_enabled: bool = True) -> list[DoctorCheck]:
    section = "Paths"

    # "Never set up" and "something disappeared" are different problems and deserve
    # different answers. Every root missing means the app has simply not been run
    # yet — one actionable line beats nine identical failures. A *partially*
    # missing layout is the alarming case (a mistyped path, a deleted library, an
    # unmounted disk) and is reported root by root below.
    roots = (paths.workspace_root, paths.library_root, paths.state_root)
    if not any(root.exists() for root in roots):
        return [
            DoctorCheck(
                section,
                "layout",
                STATUS_FAIL,
                "not created yet — run `procrafiler setup` (or `init-layout`). "
                f"Expected the inbox at {paths.workspace_root} and the library at {paths.library_root}",
            )
        ]

    results = [
        _check_path_writable(section, "workspace_root", paths.workspace_root),
        _check_path_writable(section, "inbox_dir", paths.inbox_dir),
        _check_path_writable(section, "queue_dir", paths.queue_dir),
        _check_path_writable(section, "inbox_trash_manual_dir", paths.inbox_trash_manual_dir),
        _check_path_writable(section, "library_root", paths.library_root),
        _check_path_writable(section, "library_trash_manual_dir", paths.library_trash_manual_dir),
    ]
    # The mirror is optional. When it is disabled (mirror_sync off) its folders
    # are not expected to exist — report them as skipped, not failed.
    if mirror_enabled:
        results.append(_check_path_writable(section, "mirror_root", paths.mirror_root))
        results.append(_check_path_writable(section, "mirror_trash_dir", paths.mirror_trash_dir))
    else:
        results.append(DoctorCheck(section, "mirror_root", STATUS_SKIP, "mirror disabled (mirror_sync off)"))
    results.append(_check_path_writable(section, "state_root", paths.state_root))
    return results


def check_layout(paths: RuntimePaths, *, mirror_enabled: bool = True) -> list[DoctorCheck]:
    """FAIL on a layout where one configured root sits inside another, and WARN when
    the mirror shares a disk with the library.

    `setup` now refuses a nested layout, but it never re-validated an EXISTING
    configuration — one hand-edited into the env file, or created before the guard
    existed. This is the command a user runs to decide whether to trust the app, so
    it must re-check the configuration every time, not just at creation.
    """
    section = "Layout"
    results: list[DoctorCheck] = []

    conflicts = layout_conflicts(paths, include_mirror=mirror_enabled)
    if conflicts:
        results.extend(
            DoctorCheck(section, "roots_not_nested", STATUS_FAIL, conflict) for conflict in conflicts
        )
    else:
        results.append(DoctorCheck(section, "roots_not_nested", STATUS_OK, "no overlapping roots"))

    if not mirror_enabled:
        results.append(
            DoctorCheck(section, "mirror_separate_disk", STATUS_SKIP, "mirror disabled (mirror_sync off)")
        )
        return results

    # A mirror on the same device does not survive that device failing — the whole
    # point of having one. `setup` says this once at creation and never again.
    if on_same_disk(paths.mirror_root, paths.library_root):
        results.append(
            DoctorCheck(
                section,
                "mirror_separate_disk",
                STATUS_WARN,
                "the mirror is on the SAME disk as the library — it will not protect "
                "against that disk failing",
            )
        )
    else:
        results.append(
            DoctorCheck(section, "mirror_separate_disk", STATUS_OK, "mirror is on a different disk")
        )
    return results


def check_queue(paths: RuntimePaths) -> list[DoctorCheck]:
    """FAIL when documents sit in the Queue.

    The Queue is a transient staging area: a file is there only between leaving the
    Inbox and being filed. Anything still there means a previous run was interrupted
    (Ctrl-C, SIGKILL, OOM, power loss) — those documents are invisible to the user
    until the next `process-*` recovers them, so `doctor` must NOT report a clean
    bill of health while they wait. This is the check that makes the loss visible.
    """
    section = "Queue"
    if not paths.queue_dir.is_dir():
        return [DoctorCheck(section, "queue_empty", STATUS_SKIP, "no queue directory yet")]
    try:
        stranded = sorted(p.name for p in paths.queue_dir.iterdir() if p.is_file())
    except OSError as exc:
        return [DoctorCheck(section, "queue_empty", STATUS_WARN, f"cannot read: {exc}")]

    if not stranded:
        return [DoctorCheck(section, "queue_empty", STATUS_OK, "empty (no interrupted run)")]

    shown = ", ".join(stranded[:5]) + (f", … (+{len(stranded) - 5})" if len(stranded) > 5 else "")
    return [
        DoctorCheck(
            section,
            "queue_empty",
            STATUS_FAIL,
            f"{len(stranded)} file(s) stranded by an interrupted run: {shown} "
            "— run `procrafiler process-all` to recover them into the Inbox",
        )
    ]


def check_env(paths: RuntimePaths) -> list[DoctorCheck]:
    section = "Env"
    results: list[DoctorCheck] = []

    loaded_from = os.environ.get("PROCRAFILER_ENV_LOADED_FROM", "")
    if loaded_from:
        results.append(DoctorCheck(section, "env_file_loaded", STATUS_OK, loaded_from))

        # Permissions check — file with API keys should not be world-readable.
        try:
            mode = stat.S_IMODE(os.stat(loaded_from).st_mode)
        except OSError as exc:
            results.append(
                DoctorCheck(section, "env_file_permissions", STATUS_WARN, f"could not stat: {exc}")
            )
        else:
            if mode & 0o077:
                results.append(
                    DoctorCheck(
                        section,
                        "env_file_permissions",
                        STATUS_WARN,
                        f"too permissive: 0o{mode:03o} (expected 0o600 or 0o640)",
                    )
                )
            else:
                results.append(
                    DoctorCheck(section, "env_file_permissions", STATUS_OK, f"0o{mode:03o}")
                )
    elif (explicit := os.environ.get("PROCRAFILER_ENV_FILE", "").strip()):
        # An explicit file was named and could not be read. That is a FAIL, not a
        # shrug: the run is silently using built-in defaults instead of the
        # configuration the user pointed at (a typo'd path, a bad permission).
        results.append(
            DoctorCheck(
                section,
                "env_file_loaded",
                STATUS_FAIL,
                f"PROCRAFILER_ENV_FILE points at {explicit}, which could not be read — "
                "no configuration was loaded (built-in defaults in use)",
            )
        )
    else:
        results.append(
            DoctorCheck(
                section,
                "env_file_loaded",
                STATUS_WARN,
                "no env file loaded — built-in defaults in use",
            )
        )

    return results


def check_ai_config() -> list[DoctorCheck]:
    section = "AI"
    results: list[DoctorCheck] = []

    mistral_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    uses_mistral = False

    for task in SUPPORTED_AI_TASKS:
        chain = task_chain_from_env(task)
        if not chain:
            results.append(
                DoctorCheck(section, f"task_{task.lower()}", STATUS_WARN, "no provider chain configured")
            )
            continue

        providers = ",".join(f"{e.provider}:{e.model}" for e in chain)
        results.append(
            DoctorCheck(section, f"task_{task.lower()}", STATUS_OK, providers)
        )
        if any(e.provider == "mistral" for e in chain):
            uses_mistral = True

    if uses_mistral:
        if mistral_key:
            results.append(DoctorCheck(section, "mistral_api_key", STATUS_OK, "set"))
        else:
            results.append(
                DoctorCheck(
                    section,
                    "mistral_api_key",
                    STATUS_FAIL,
                    "MISTRAL_API_KEY is unset but at least one task chain uses mistral",
                )
            )
    else:
        results.append(
            DoctorCheck(
                section,
                "mistral_api_key",
                STATUS_SKIP,
                "no task chain uses mistral",
            )
        )

    return results


def _config_dir(paths: RuntimePaths) -> Path:
    """Where the user's own pricing.json would be."""
    return paths.settings_file.parent


def check_pricing(paths: RuntimePaths) -> list[DoctorCheck]:
    """Can the models this installation calls actually be priced?

    Worth its own section because the answer changes without anybody touching
    ProcraFiler. The published table keys each rate by the name on the seller's own
    page, generation number included, and those move — `ocr 4` became `ocr 4.1` in
    days. Resolution reads the generation from the feed for exactly that reason, but
    a seller can still restructure a family beyond what any rule follows, and then
    the run forecast quietly stops being able to quote a total.

    Deliberately a WARN and never a FAIL. A missing price is not a reason to refuse
    to file documents, and the weekly refresh is deliberately NOT tightened to
    reject such a table: doing so would stop every price update, for every model,
    the day one name moved. This check is the other half of that decision — the loss
    is made loud here instead of being prevented there.
    """
    section = "Pricing"
    results: list[DoctorCheck] = []

    table = load_price_table(_config_dir(paths))
    if table is None:
        return [DoctorCheck(section, "price_table", STATUS_WARN, "no usable price table")]
    results.append(DoctorCheck(section, "price_table", STATUS_OK, table.origin))

    for task in SUPPORTED_AI_TASKS:
        chain = task_chain_from_env(task)
        if not chain:
            continue
        entry = chain[0]
        name = f"price_{task.lower()}"
        if entry.provider in LOCAL_PROVIDERS:
            results.append(DoctorCheck(section, name, STATUS_SKIP, f"{entry.provider} runs locally, nothing billed"))
            continue

        price = table.price_for(entry.provider, entry.model)
        label = table.label_for(entry.provider, entry.model)
        if price is None or not price.is_priceable:
            # No remedy is offered on purpose. Telling the user to map the model
            # would be a dead end: the feed never deletes a key, so a model it has
            # ever listed still has a price — the current one, or the last observed
            # with the date it stopped. Reaching here means the feed does not carry
            # this model at all, and no local mapping can conjure a rate.
            results.append(
                DoctorCheck(
                    section, name, STATUS_WARN,
                    f"no price for {entry.provider}:{entry.model} — a run cannot be "
                    f"costed, and its share of the total is reported as unknown. The "
                    f"published price list does not carry this model.",
                )
            )
            continue

        priced_as = f' as "{label}"' if label else ""
        if price.absent_since:
            results.append(
                DoctorCheck(
                    section, name, STATUS_WARN,
                    f"{entry.provider}:{entry.model} priced{priced_as}, but that entry left the "
                    f"seller's price list on {price.absent_since} — the rate is the last one "
                    f"observed, not a current one",
                )
            )
            continue
        results.append(DoctorCheck(section, name, STATUS_OK, f"{entry.provider}:{entry.model}{priced_as}"))

    return results


def check_catalog(paths: RuntimePaths) -> list[DoctorCheck]:
    section = "Catalog"
    db_path = paths.catalog_db_file

    if not db_path.exists() or db_path.stat().st_size == 0:
        return [
            DoctorCheck(
                section,
                "catalog_db",
                STATUS_WARN,
                "catalog.db missing or empty — will be initialized on first process command",
            )
        ]

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
            if not columns:
                return [
                    DoctorCheck(
                        section,
                        "catalog_schema",
                        STATUS_WARN,
                        "documents table missing — will be created on first process command",
                    )
                ]
            count_row = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
            count = int(count_row["n"]) if count_row is not None else 0
    except sqlite3.Error as exc:
        return [DoctorCheck(section, "catalog_db", STATUS_FAIL, f"cannot open: {exc}")]

    results = [DoctorCheck(section, "catalog_db", STATUS_OK, f"{count} documents")]
    if "flow_state" in columns:
        results.append(DoctorCheck(section, "catalog_schema", STATUS_OK, "flow_state column present"))
    else:
        results.append(
            DoctorCheck(
                section,
                "catalog_schema",
                STATUS_WARN,
                "flow_state column missing — will be added on next process command",
            )
        )
    return results


def check_runtime_lock(paths: RuntimePaths) -> list[DoctorCheck]:
    """Report whether the lock is held, without taking it and without creating it.

    Acquiring the real lock — which is what this used to do — made a diagnostic
    create the state directory and the lock file, and briefly blocked any run
    starting at that moment.
    """
    section = "Concurrency"
    holder = probe_runtime_lock(paths)
    if holder is None:
        return [DoctorCheck(section, "runtime_lock", STATUS_OK, "available")]
    return [
        DoctorCheck(
            section,
            "runtime_lock",
            STATUS_WARN,
            f"held by another process: {holder}",
        )
    ]


def run_doctor(paths: RuntimePaths) -> list[DoctorCheck]:
    try:
        mirror_enabled = bool(load_feature_settings(paths)["features"].get("mirror_sync", True))
    except Exception:
        mirror_enabled = True
    checks: list[DoctorCheck] = list(check_paths(paths, mirror_enabled=mirror_enabled))
    checks.extend(check_layout(paths, mirror_enabled=mirror_enabled))
    for fn in _CHECK_GROUPS_AFTER_PATHS:
        checks.extend(fn(paths))
    return checks


# Order matters here: path checks first (cheapest, most likely to fail), then the
# Queue (a stranded-file FAIL the user must see early), then env/AI (config), then
# catalog (touches disk), then lock (briefly acquires). `check_paths` runs first in
# `run_doctor` (it needs the mirror flag).
_CHECK_GROUPS_AFTER_PATHS: tuple[Callable[[RuntimePaths], list[DoctorCheck]], ...] = (
    check_queue,
    check_env,
    lambda _paths: check_ai_config(),
    check_pricing,
    check_catalog,
    check_runtime_lock,
)


def format_report(checks: list[DoctorCheck]) -> str:
    """Produce a human-readable, section-grouped report."""
    lines: list[str] = ["ProcraFiler doctor"]
    current_section = ""
    for check in checks:
        if check.section != current_section:
            current_section = check.section
            lines.append("")
            lines.append(current_section)
            lines.append("-" * len(current_section))
        lines.append(f"[{check.status}] {check.name}: {check.message}")

    counts = {s: 0 for s in (STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP)}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    lines.append("")
    lines.append("Summary")
    lines.append("-------")
    lines.append(
        f"{len(checks)} checks: "
        f"{counts[STATUS_OK]} OK, "
        f"{counts[STATUS_WARN]} WARN, "
        f"{counts[STATUS_FAIL]} FAIL, "
        f"{counts[STATUS_SKIP]} SKIP"
    )
    return "\n".join(lines)


def overall_exit_code(checks: list[DoctorCheck]) -> int:
    return 1 if any(c.status == STATUS_FAIL for c in checks) else 0
