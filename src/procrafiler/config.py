from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from procrafiler.dev_guard import (  # type: ignore[reportMissingImports]
    guard_mutation,
    is_marked_sandbox,
    mark_sandbox,
    source_checkout_root,
)
from procrafiler.taxonomy import ensure_base_library_directories  # type: ignore[reportMissingImports]


FEATURE_NAMES = ("actions_log", "catalog_snapshot", "mirror_sync")


@dataclass(frozen=True)
class RuntimePaths:
    workspace_root: Path
    inbox_dir: Path
    queue_dir: Path
    inbox_trash_manual_dir: Path
    library_root: Path
    library_trash_manual_dir: Path
    mirror_root: Path
    mirror_trash_dir: Path
    state_root: Path
    actions_log_file: Path
    catalog_db_file: Path
    catalog_snapshot_file: Path
    search_index_file: Path
    settings_file: Path
    policy_file: Path


@dataclass(frozen=True)
class RuntimePolicy:
    mirror_retention_days: int
    mirror_versions_keep: int
    taxonomy_max_depth: int


# Where `sandbox/run.sh` puts a development run. Duplicated here rather than read
# from the script, and a test asserts the two agree: two spellings of "the sandbox"
# that drifted apart would give a checkout two sandboxes, which is the very
# collision this is meant to remove.
SANDBOX_WORKSPACE = ("sandbox", "workspace")


def _home_defaults() -> dict[str, Path]:
    """What an unconfigured PRODUCTION run targets. Always the user's home.

    Kept as its own function because `dev_guard` needs exactly this answer — "are
    these the roots a real user's installation would use?" — regardless of what the
    process it is guarding happens to default to.
    """
    home = Path.home()
    return {
        "workspace": home / "Downloads" / "ProcraFiler_Inbox",
        "library": home / "ProcraFiler_Library",
        "mirror": home / "ProcraFiler_Library_Mirror",
        "state": home / ".local" / "share" / "procrafiler",
        "config": home / ".config" / "procrafiler",
    }


def _checkout_defaults(checkout: Path) -> dict[str, Path]:
    """What an unconfigured run FROM A SOURCE CHECKOUT targets: its own sandbox.

    The point is to stop naming the home at all rather than to name it and refuse.
    Until this existed, a development run with no environment set computed the real
    library's path and was then turned away by `dev_guard` — safe, but one guard
    away from the 2026-07-28 incident. Now there is nothing to turn away, and the
    guards remain underneath as the net (see `dev-prod-isolation.md`).

    Deliberately identical to `sandbox/run.sh`, so a run started through the script
    and one started without it share one sandbox instead of quietly creating two.
    """
    work = checkout.joinpath(*SANDBOX_WORKSPACE)
    return {
        "workspace": work / "ProcraFiler_Inbox",
        "library": work / "ProcraFiler_Library",
        "mirror": work / "ProcraFiler_Library_Mirror",
        "state": work / "state",
        "config": work / "config",
    }


def default_runtime_paths(*, force_home_defaults: bool = False) -> RuntimePaths:
    """Build default runtime paths, overridable through environment variables.

    `force_home_defaults` asks the production question — "where would a real user's
    installation put these?" — from a process that may be a source checkout. Only
    `dev_guard` needs it, and it needs it to keep working: without it, guard C would
    compare a checkout's roots against the checkout's own sandbox and always agree.
    """
    checkout = None if force_home_defaults else source_checkout_root()
    fallback = _home_defaults() if checkout is None else _checkout_defaults(checkout)

    workspace_root = Path(os.environ.get("PROCRAFILER_WORKSPACE_DIR", str(fallback["workspace"])))
    library_root = Path(os.environ.get("PROCRAFILER_LIBRARY_DIR", str(fallback["library"])))
    mirror_root = Path(os.environ.get("PROCRAFILER_LIBRARY_MIRROR_DIR", str(fallback["mirror"])))
    state_root = Path(os.environ.get("PROCRAFILER_HOME", str(fallback["state"])))
    config_root = Path(os.environ.get("PROCRAFILER_CONFIG_HOME", str(fallback["config"])))

    return RuntimePaths(
        workspace_root=workspace_root,
        inbox_dir=workspace_root / "Inbox",
        queue_dir=workspace_root / "Queue",
        inbox_trash_manual_dir=workspace_root / "Inbox_Trash_Manual",
        library_root=library_root,
        library_trash_manual_dir=Path(str(library_root) + "_Trash_Manual"),
        mirror_root=mirror_root,
        mirror_trash_dir=mirror_root / "Mirror_Trash",
        state_root=state_root,
        actions_log_file=state_root / "actions_log.jsonl",
        catalog_db_file=state_root / "catalog.db",
        catalog_snapshot_file=state_root / "catalog_snapshot.json",
        search_index_file=state_root / "search_index.db",
        settings_file=config_root / "settings.json",
        policy_file=config_root / "policy.toml",
    )


# The configured roots that must never equal or contain one another.
LAYOUT_ROOTS: tuple[tuple[str, str], ...] = (
    ("Inbox workspace", "workspace_root"),
    ("Library", "library_root"),
    ("Library trash", "library_trash_manual_dir"),
    ("Mirror", "mirror_root"),
    ("App state", "state_root"),
)


def layout_conflicts(paths: RuntimePaths, *, include_mirror: bool = True) -> list[str]:
    """Human-readable descriptions of configured roots that EQUAL or CONTAIN each
    other. Empty list = a sane layout.

    Nesting must be refused, not merely warned about, because the code assumes it
    cannot happen. `rescan.walk_library_files` says so in writing — "the library's
    trash and the mirror live OUTSIDE library_root already" — which is true of the
    DEFAULTS only. Put the mirror inside the library and the library walk swallows
    the mirror: its copies get renamed, phantom duplicate rows enter the catalog,
    a `Mirror/Mirror/` level appears, and every unknown mirror file costs a paid AI
    call. `setup` accepted this silently (it only compared paths for exact
    equality), so the guard belongs here, shared by `setup` and `doctor`.

    Paths are compared RESOLVED, so `~/lib` and `~/./lib/` are the same place, and
    a symlinked library cannot smuggle a nested root past the check.
    """
    entries: list[tuple[str, Path]] = []
    for label, attr in LAYOUT_ROOTS:
        if attr == "mirror_root" and not include_mirror:
            continue
        raw = getattr(paths, attr)
        try:
            entries.append((label, Path(raw).expanduser().resolve()))
        except OSError:
            continue

    conflicts: list[str] = []
    for i, (label_a, path_a) in enumerate(entries):
        for label_b, path_b in entries[i + 1 :]:
            if path_a == path_b:
                conflicts.append(f"{label_a} and {label_b} are the same folder: {path_a}")
            elif path_b.is_relative_to(path_a):
                conflicts.append(f"{label_b} is inside {label_a}: {path_b} is under {path_a}")
            elif path_a.is_relative_to(path_b):
                conflicts.append(f"{label_a} is inside {label_b}: {path_a} is under {path_b}")
    return conflicts


def default_runtime_policy() -> RuntimePolicy:
    return RuntimePolicy(
        mirror_retention_days=30,
        mirror_versions_keep=3,
        # A safety net against a runaway/hallucinated AI folder path, not a
        # design constraint on classification. High on purpose; tune in
        # policy.toml if ever needed.
        taxonomy_max_depth=10,
    )


def _policy_to_toml(policy: RuntimePolicy) -> str:
    return (
        "[mirror]\n"
        f"retention_days = {policy.mirror_retention_days}\n"
        f"versions_keep = {policy.mirror_versions_keep}\n\n"
        "[taxonomy]\n"
        f"max_depth = {policy.taxonomy_max_depth}\n"
    )


def save_runtime_policy(paths: RuntimePaths, policy: RuntimePolicy) -> None:
    paths.policy_file.parent.mkdir(parents=True, exist_ok=True)
    paths.policy_file.write_text(_policy_to_toml(policy), encoding="utf-8")


def load_runtime_policy(paths: RuntimePaths) -> RuntimePolicy:
    policy = default_runtime_policy()
    if not paths.policy_file.exists():
        return policy

    try:
        loaded: Any = tomllib.loads(paths.policy_file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return policy

    if not isinstance(loaded, dict):
        return policy

    loaded_dict = cast(dict[str, object], loaded)
    mirror_obj = loaded_dict.get("mirror")
    taxonomy_obj = loaded_dict.get("taxonomy")

    mirror = cast(dict[str, object], mirror_obj) if isinstance(mirror_obj, dict) else {}
    taxonomy = cast(dict[str, object], taxonomy_obj) if isinstance(taxonomy_obj, dict) else {}

    retention = mirror.get("retention_days")
    versions_keep = mirror.get("versions_keep")
    max_depth = taxonomy.get("max_depth")

    mirror_retention_days = retention if isinstance(retention, int) and retention > 0 else policy.mirror_retention_days
    mirror_versions_keep = (
        versions_keep if isinstance(versions_keep, int) and versions_keep > 0 else policy.mirror_versions_keep
    )
    taxonomy_max_depth = max_depth if isinstance(max_depth, int) and max_depth > 0 else policy.taxonomy_max_depth

    return RuntimePolicy(
        mirror_retention_days=mirror_retention_days,
        mirror_versions_keep=mirror_versions_keep,
        taxonomy_max_depth=taxonomy_max_depth,
    )


def default_feature_settings() -> dict[str, dict[str, bool]]:
    return {
        "features": {
            "actions_log": True,
            "catalog_snapshot": True,
            "mirror_sync": True,
        }
    }


# How a hand-deleted library document is recorded in the catalog (see the
# `deletion-mode` command). `tombstone` keeps id + content hash + deletion date
# (so a later re-deposit is recognised); `purge` keeps nothing of the document
# (the deletion survives only in the action log).
DELETION_MODES = ("tombstone", "purge")
DEFAULT_DELETION_MODE = "tombstone"


def _read_settings_file(paths: RuntimePaths) -> dict[str, object]:
    """The whole settings file as a raw dict, tolerant of missing/corrupt JSON.
    Used so feature flags and the deletion mode share one file without one
    overwriting the other's keys."""
    if not paths.settings_file.exists():
        return {}
    try:
        loaded: Any = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}


def _write_settings_file(paths: RuntimePaths, data: dict[str, object]) -> None:
    paths.settings_file.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def save_feature_settings(paths: RuntimePaths, settings: dict[str, dict[str, bool]]) -> None:
    # Merge into the existing file so other settings (e.g. deletion_mode) survive.
    data = _read_settings_file(paths)
    data["features"] = settings["features"]
    _write_settings_file(paths, data)


# The user's primary language (a short code like "fr", "en", "es"), used to
# enrich the catalog with translations so search works in the user's language and
# English. Default English (the base taxonomy's language).
DEFAULT_USER_LANGUAGE = "en"
_LANGUAGE_RE = re.compile(r"^[a-z]{2,8}(-[a-z]{2,8})?$")


def get_user_language(paths: RuntimePaths) -> str:
    """The user's primary language. An explicit setting (from `setup-context` or
    the `language` command) always wins; otherwise it is **auto-detected** from
    the languages of the user's own catalogued documents — so a French user's
    library works in French with zero configuration. Falls back to English only
    when neither is available (e.g. an empty catalog)."""
    value = _read_settings_file(paths).get("user_language")
    if isinstance(value, str) and _LANGUAGE_RE.match(value):
        return value
    from procrafiler.catalog import CatalogRepository  # local import: avoid import cycle
    detected = CatalogRepository(paths.catalog_db_file).majority_language()
    return detected if detected and _LANGUAGE_RE.match(detected) else DEFAULT_USER_LANGUAGE


def set_user_language(paths: RuntimePaths, language: str) -> str:
    code = language.strip().lower()
    if not _LANGUAGE_RE.match(code):
        raise ValueError(f"Invalid language code: {language!r} (expected e.g. 'fr', 'en', 'pt-br')")
    data = _read_settings_file(paths)  # preserve features / deletion_mode
    data["user_language"] = code
    _write_settings_file(paths, data)
    return code


def get_deletion_mode(paths: RuntimePaths) -> str:
    mode = _read_settings_file(paths).get("deletion_mode")
    return mode if mode in DELETION_MODES else DEFAULT_DELETION_MODE


def set_deletion_mode(paths: RuntimePaths, mode: str) -> str:
    if mode not in DELETION_MODES:
        raise ValueError(f"Unknown deletion mode: {mode!r} (expected one of {DELETION_MODES})")
    data = _read_settings_file(paths)  # preserve features and anything else
    data["deletion_mode"] = mode
    _write_settings_file(paths, data)
    return mode


def load_feature_settings(paths: RuntimePaths) -> dict[str, dict[str, bool]]:
    if not paths.settings_file.exists():
        return default_feature_settings()

    try:
        loaded: Any = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        loaded = default_feature_settings()

    if not isinstance(loaded, dict):
        return default_feature_settings()

    loaded_dict = cast(dict[str, object], loaded)

    features_obj = loaded_dict.get("features")
    if not isinstance(features_obj, dict):
        return default_feature_settings()

    features_dict = cast(dict[str, object], features_obj)

    normalized = default_feature_settings()
    for key in FEATURE_NAMES:
        value = features_dict.get(key)
        if isinstance(value, bool):
            normalized["features"][key] = value
    return normalized


def set_feature_flag(paths: RuntimePaths, feature: str, enabled: bool) -> dict[str, dict[str, bool]]:
    if feature not in FEATURE_NAMES:
        raise ValueError(f"Unknown feature: {feature}")

    settings = load_feature_settings(paths)
    settings["features"][feature] = enabled
    save_feature_settings(paths, settings)
    return settings


# What a purge may delete: regenerable state and the app's own configuration.
# NEVER the library, its mirror, the trashes or the inbox — those are the documents
# themselves, and no uninstaller has any business reaching them.
_PURGEABLE_FILES = (
    "actions_log_file",
    "catalog_db_file",
    "catalog_snapshot_file",
    "search_index_file",
    "settings_file",
    "policy_file",
)

_PRESERVED_ROOTS = (
    "workspace_root",
    "library_root",
    "library_trash_manual_dir",
    "mirror_root",
    "mirror_trash_dir",
)

# The user's own writing, sitting in the app's config directory: who they are, what
# they do, the names that mean their work. A purge removes it — leaving personal
# notes behind on a machine somebody has just wiped the app from is not caution, it
# is a leak. But it is theirs, so the uninstaller OFFERS to copy it out first and
# says where the copy went. Offered, never imposed: a copy nobody asked for is the
# same leak under another name.
_PERSONAL_FILENAMES = ("context.txt", "context.md")

PATHS_REPORT_SCHEMA = 2


def layout_mode(paths: RuntimePaths) -> str:
    """Which kind of layout this is: `sandbox`, `dev` or `prod`.

    Derived, never configured. `dev_guard` already answers "is this package a source
    checkout?" by where it was imported from, and that fact is true without anyone
    having to remember to declare it — unlike a mode flag, which would be wrong
    precisely when it mattered.
    """
    if is_marked_sandbox(paths):
        return "sandbox"
    return "dev" if source_checkout_root() is not None else "prod"


def paths_report(paths: RuntimePaths) -> dict[str, object]:
    """Every runtime path, for the install scripts — so they stop restating this
    module in bash.

    They had no other option and they drifted: the purge list came to name a
    `search_index.db` a given layout never had, while missing the runtime lock, the
    state directory itself and every subdirectory under it. A shell script cannot
    import this module, so it reads the answer instead of reproducing it.

    `purge_dirs` carries the state and config ROOTS, not merely the files inside
    them. A purge that removes four files and leaves the directory, its lock and
    four stale subdirectories has not purged anything a user would recognise.
    """
    fields = {key: str(value) for key, value in vars(paths).items()}
    config_root = paths.settings_file.parent
    return {
        "schema": PATHS_REPORT_SCHEMA,
        "version": _package_version(),
        "mode": layout_mode(paths),
        "paths": fields,
        "purge_files": [fields[name] for name in _PURGEABLE_FILES],
        # The state root goes whole — everything under it is the app's own memory,
        # and this is where the stale subdirectories of old versions accumulate.
        # The CONFIG root does NOT: it also holds the user's own context file, which
        # must not be swept away by a directory-wide `rm -rf` without them being
        # asked. Its files are named one by one, and the directory is removed only
        # if it ends up empty.
        "purge_dirs": [str(paths.state_root)],
        # Purged like the rest, but only after the user has been offered a copy.
        # Listed apart precisely so the uninstaller cannot delete them silently.
        "personal_files": [str(config_root / name) for name in _PERSONAL_FILENAMES],
        "preserve": [fields[name] for name in _PRESERVED_ROOTS],
    }


def _package_version() -> str:
    from procrafiler import __version__  # local: keeps this module import-light

    return __version__


def format_paths_json(paths: RuntimePaths) -> str:
    return json.dumps(paths_report(paths), indent=2, sort_keys=True)


def ensure_runtime_layout(paths: RuntimePaths, *, include_mirror: bool = True) -> None:
    """Create the layout, after refusing to do so outside a development sandbox.

    The guard sits here rather than in the CLI because this is where the damage
    happens: ~30 entry points call this function, and so does any ad-hoc script
    that drives the pipeline directly — which is exactly how a development run
    once materialised a full layout in the developer's real home directory. One
    choke point, no entry point to forget. See `dev_guard`.
    """
    guard_mutation(paths)
    # Imported here, not at module level: `state_version` needs `RuntimePaths`, and
    # this module is what defines it.
    from procrafiler.state_version import (  # type: ignore[reportMissingImports]
        guard_state_version,
        record_state_version,
    )

    # Before anything is created or touched, for the same reason `guard_mutation`
    # runs first: a refusal that arrives after the writing has begun is not one.
    guard_state_version(paths)

    directories = [
        paths.workspace_root,
        paths.inbox_dir,
        paths.queue_dir,
        paths.inbox_trash_manual_dir,
        paths.library_root,
        paths.library_trash_manual_dir,
        paths.state_root,
        paths.settings_file.parent,
    ]
    # The mirror is optional (a backup copy of the library). When the user
    # declined it at setup — i.e. the `mirror_sync` feature is off — we must NOT
    # create its folders, otherwise an empty mirror would orphan on disk.
    if include_mirror:
        directories.extend((paths.mirror_root, paths.mirror_trash_dir))

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    for file_path in (paths.actions_log_file, paths.catalog_db_file, paths.catalog_snapshot_file):
        file_path.touch(exist_ok=True)

    ensure_base_library_directories(paths.library_root)

    if not paths.settings_file.exists():
        save_feature_settings(paths, default_feature_settings())
    if not paths.policy_file.exists():
        save_runtime_policy(paths, default_runtime_policy())

    # Claim this layout as a development sandbox now that its state root exists.
    # A sandbox fills up with test documents like any library, so without this the
    # "already holds real work" guard would start refusing it on the second run.
    if source_checkout_root() is not None:
        mark_sandbox(paths)

    # Last, and only once the state root exists: say which release owns this state,
    # so the next older one meets a refusal instead of an open door.
    record_state_version(paths)
