from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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
    settings_file: Path
    policy_file: Path


@dataclass(frozen=True)
class RuntimePolicy:
    mirror_retention_days: int
    mirror_versions_keep: int
    taxonomy_max_depth: int


def default_runtime_paths() -> RuntimePaths:
    """Build default runtime paths, overridable through environment variables."""
    workspace_root = Path(
        os.environ.get(
            "PROCRAFILER_WORKSPACE_DIR",
            str(Path.home() / "Downloads" / "ProcraFiler_Inbox"),
        )
    )
    library_root = Path(
        os.environ.get(
            "PROCRAFILER_LIBRARY_DIR",
            str(Path.home() / "ProcraFiler_Library"),
        )
    )
    mirror_root = Path(
        os.environ.get(
            "PROCRAFILER_LIBRARY_MIRROR_DIR",
            str(Path.home() / "ProcraFiler_Library_Mirror"),
        )
    )
    state_root = Path(os.environ.get("PROCRAFILER_HOME", str(Path.home() / ".local" / "share" / "procrafiler")))
    config_root = Path(os.environ.get("PROCRAFILER_CONFIG_HOME", str(Path.home() / ".config" / "procrafiler")))

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
        settings_file=config_root / "settings.json",
        policy_file=config_root / "policy.toml",
    )


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


def ensure_runtime_layout(paths: RuntimePaths) -> None:
    for directory in (
        paths.workspace_root,
        paths.inbox_dir,
        paths.queue_dir,
        paths.inbox_trash_manual_dir,
        paths.library_root,
        paths.library_trash_manual_dir,
        paths.mirror_root,
        paths.mirror_trash_dir,
        paths.state_root,
        paths.settings_file.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    for file_path in (paths.actions_log_file, paths.catalog_db_file, paths.catalog_snapshot_file):
        file_path.touch(exist_ok=True)

    ensure_base_library_directories(paths.library_root)

    if not paths.settings_file.exists():
        save_feature_settings(paths, default_feature_settings())
    if not paths.policy_file.exists():
        save_runtime_policy(paths, default_runtime_policy())
