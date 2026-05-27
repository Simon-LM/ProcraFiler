from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

FAKE_NOW_ENV_VAR = "PROCRAFILER_FAKE_NOW"


def _resolve_now_utc() -> datetime:
    """Return current UTC time, honoring PROCRAFILER_FAKE_NOW for tests.

    The fake value must be an ISO-8601 string ending with 'Z' or an explicit
    offset. Any parse error falls back to real time silently — this is a test
    affordance, not a feature, and must never block production.
    """
    raw = os.environ.get(FAKE_NOW_ENV_VAR, "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)

from procrafiler import __version__
from procrafiler.config import (
    FEATURE_NAMES,
    default_runtime_paths,
    load_runtime_policy,
    ensure_runtime_layout,
    load_feature_settings,
    set_feature_flag,
)
from procrafiler.mirror import purge_mirror_trash  # type: ignore[reportMissingImports]
from procrafiler.pipeline import process_all_inbox_files, process_next_inbox_file
from procrafiler.runtime_env import load_runtime_env  # type: ignore[reportMissingImports]
from procrafiler.runtime_lock import RuntimeLockedError, runtime_lock

EXIT_TEMPFAIL = 75  # sysexits.h: temp resource shortage; reused for "lock held"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="procrafiler",
        description="ProcraFiler CLI - AI-assisted file organization with explicit safety guardrails",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show workspace and state paths with feature status")
    subparsers.add_parser("init-layout", help="Create workspace, state folders, and metadata files")
    subparsers.add_parser("features", help="List feature flags")
    subparsers.add_parser("policy-effective", help="Show effective runtime policy values")

    process_once = subparsers.add_parser("process-once", help="Process one file from Inbox")
    process_once.add_argument("--dry-run", action="store_true", help="Simulate one processing cycle")

    process_all = subparsers.add_parser("process-all", help="Process all files currently present in Inbox")
    process_all.add_argument("--dry-run", action="store_true", help="Simulate batch processing")

    purge_trash = subparsers.add_parser("purge-mirror-trash", help="Purge old files from Mirror_Trash by TTL")
    purge_trash.add_argument("--days", type=int, default=None, help="Retention period in days (default: policy)")

    feature_set = subparsers.add_parser("feature-set", help="Enable or disable one feature")
    feature_set.add_argument("feature", choices=list(FEATURE_NAMES), help="Feature name")
    feature_set.add_argument("state", choices=["on", "off"], help="Target state")

    return parser


def cmd_status() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    settings = load_feature_settings(paths)
    policy = load_runtime_policy(paths)

    print("ProcraFiler status")
    print("Workspace")
    print(f"- workspace_root: {paths.workspace_root}")
    print(f"- inbox_dir: {paths.inbox_dir}")
    print(f"- queue_dir: {paths.queue_dir}")
    print(f"- inbox_trash_manual_dir: {paths.inbox_trash_manual_dir}")
    print(f"- library_root: {paths.library_root}")
    print(f"- library_trash_manual_dir: {paths.library_trash_manual_dir}")
    print(f"- mirror_root: {paths.mirror_root}")
    print(f"- mirror_trash_dir: {paths.mirror_trash_dir}")
    print("State")
    print(f"- state_root: {paths.state_root}")
    print(f"- actions_log_file: {paths.actions_log_file}")
    print(f"- catalog_db_file: {paths.catalog_db_file}")
    print(f"- catalog_snapshot_file: {paths.catalog_snapshot_file}")
    print(f"- settings_file: {paths.settings_file}")
    print(f"- env_loaded_from: {os.environ.get('PROCRAFILER_ENV_LOADED_FROM', 'none')}")
    print("Features")
    print(f"- actions_log: {settings['features']['actions_log']}")
    print(f"- catalog_snapshot: {settings['features']['catalog_snapshot']}")
    print(f"- mirror_sync: {settings['features']['mirror_sync']}")
    print("Policy")
    print(f"- mirror_retention_days: {policy.mirror_retention_days}")
    print(f"- mirror_versions_keep: {policy.mirror_versions_keep}")
    print(f"- taxonomy_max_depth: {policy.taxonomy_max_depth}")
    return 0


def cmd_init_layout() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)

    print("Runtime layout initialized:")
    print(f"- {paths.workspace_root}")
    print(f"- {paths.inbox_dir}")
    print(f"- {paths.queue_dir}")
    print(f"- {paths.inbox_trash_manual_dir}")
    print(f"- {paths.library_root}")
    print(f"- {paths.library_trash_manual_dir}")
    print(f"- {paths.mirror_root}")
    print(f"- {paths.mirror_trash_dir}")
    print(f"- {paths.state_root}")
    print(f"- {paths.actions_log_file}")
    print(f"- {paths.catalog_db_file}")
    print(f"- {paths.catalog_snapshot_file}")
    print(f"- {paths.settings_file}")
    return 0


def cmd_features() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    settings = load_feature_settings(paths)

    print("ProcraFiler features")
    print(f"- actions_log: {settings['features']['actions_log']}")
    print(f"- catalog_snapshot: {settings['features']['catalog_snapshot']}")
    print(f"- mirror_sync: {settings['features']['mirror_sync']}")
    return 0


def cmd_policy_effective() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    policy = load_runtime_policy(paths)

    print("ProcraFiler policy effective")
    print(f"- policy_file: {paths.policy_file}")
    print(f"- mirror_retention_days: {policy.mirror_retention_days}")
    print(f"- mirror_versions_keep: {policy.mirror_versions_keep}")
    print(f"- taxonomy_max_depth: {policy.taxonomy_max_depth}")
    return 0


def cmd_feature_set(feature: str, state: str) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    enabled = state == "on"
    settings = set_feature_flag(paths, feature, enabled)

    print(f"Updated feature '{feature}' -> {enabled}")
    print("Current feature flags:")
    print(f"- actions_log: {settings['features']['actions_log']}")
    print(f"- catalog_snapshot: {settings['features']['catalog_snapshot']}")
    print(f"- mirror_sync: {settings['features']['mirror_sync']}")
    return 0


def _print_lock_busy(err: RuntimeLockedError) -> None:
    print(f"ProcraFiler is already running, aborting. Lock file: {err.lock_path}", file=sys.stderr)


def cmd_process_once(dry_run: bool = False) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    try:
        with runtime_lock(paths):
            status = process_next_inbox_file(paths, dry_run=dry_run)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(f"Pipeline result: {status}")
    return 0


def cmd_process_all(dry_run: bool = False) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    try:
        with runtime_lock(paths):
            summary = process_all_inbox_files(paths, dry_run=dry_run)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(
        "Batch result: "
        f"processed: {summary['processed']}, "
        f"duplicates: {summary['duplicates']}, "
        f"manual_reviews: {summary['manual_reviews']}, "
        f"errors: {summary['errors']}, "
        f"mirror_failures: {summary['mirror_failures']}"
    )
    return 0


def cmd_purge_mirror_trash(days: int | None) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)

    if days is None:
        days = load_runtime_policy(paths).mirror_retention_days

    now_utc = _resolve_now_utc()
    try:
        with runtime_lock(paths):
            removed = purge_mirror_trash(paths, retention_days=days, now_utc=now_utc)

            features = load_feature_settings(paths)["features"]
            if features.get("actions_log", True):
                event = {
                    "event_id": str(uuid4()),
                    "event_time_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "operation_id": str(uuid4()),
                    "action": "mirror_trash_purge",
                    "status": "success",
                    "message": f"Mirror trash purge completed, removed: {removed}",
                    "retention_days": days,
                    "removed_count": removed,
                }
                with paths.actions_log_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=True) + "\n")
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL

    print(f"Mirror trash purge completed, removed: {removed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_runtime_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        return cmd_status()
    if args.command == "init-layout":
        return cmd_init_layout()
    if args.command == "features":
        return cmd_features()
    if args.command == "policy-effective":
        return cmd_policy_effective()
    if args.command == "feature-set":
        return cmd_feature_set(args.feature, args.state)
    if args.command == "process-once":
        return cmd_process_once(args.dry_run)
    if args.command == "process-all":
        return cmd_process_all(args.dry_run)
    if args.command == "purge-mirror-trash":
        return cmd_purge_mirror_trash(args.days)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
