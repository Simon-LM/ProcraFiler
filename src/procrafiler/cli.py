from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
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
    DELETION_MODES,
    FEATURE_NAMES,
    default_runtime_paths,
    get_deletion_mode,
    get_user_language,
    load_runtime_policy,
    ensure_runtime_layout,
    load_feature_settings,
    set_deletion_mode,
    set_feature_flag,
    set_user_language,
)
from procrafiler.doctor import format_report, overall_exit_code, run_doctor
from procrafiler.mirror import purge_mirror_trash  # type: ignore[reportMissingImports]
from procrafiler.pipeline import (
    LibraryTrashError,
    enrich_keywords,
    move_library_file_to_trash,
    process_all_inbox_files,
    run_rescan,
    process_next_inbox_file,
    reconcile_catalog_snapshot,
    run_review,
)
from procrafiler.catalog import CatalogRepository
from procrafiler.runtime_env import load_runtime_env  # type: ignore[reportMissingImports]
from procrafiler.runtime_lock import RuntimeLockedError, runtime_lock
from procrafiler.scrub import format_report as format_scrub_report
from procrafiler.scrub import scrub as run_scrub

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
    subparsers.add_parser(
        "doctor",
        help="Diagnose paths, env, AI config, catalog schema, and lock state. Exit non-zero on any FAIL.",
    )

    process_once = subparsers.add_parser("process-once", help="Process one file from Inbox")
    process_once.add_argument("--dry-run", action="store_true", help="Simulate one processing cycle")

    process_all = subparsers.add_parser("process-all", help="Process all files currently present in Inbox")
    process_all.add_argument("--dry-run", action="store_true", help="Simulate batch processing")

    purge_trash = subparsers.add_parser("purge-mirror-trash", help="Purge old files from Mirror_Trash by TTL")
    purge_trash.add_argument("--days", type=int, default=None, help="Retention period in days (default: policy)")

    subparsers.add_parser(
        "reconcile-snapshot",
        help="Compare catalog_snapshot.json against catalog.db and rewrite the snapshot if out of sync",
    )

    scrub_p = subparsers.add_parser(
        "scrub",
        help="Integrity check: re-hash stored documents vs the catalog (library + mirror); exit non-zero on a problem",
    )
    scrub_p.add_argument("--limit", type=int, default=None,
                         help="Check only N documents, least-recently-verified first (default: all)")
    scrub_p.add_argument("--no-mirror", action="store_true", help="Check the library only, skip the mirror")
    scrub_p.add_argument("--repair", action="store_true",
                         help="Heal: restore a bad copy from a verified-good one (library <-> mirror)")

    subparsers.add_parser(
        "review",
        help="Resolve the decisions queue: files the AI was unsure about, with its proposed options",
    )

    library_trash = subparsers.add_parser(
        "library-trash",
        help="Move a library file to Library_Trash_Manual and quarantine its mirror copy",
    )
    library_trash.add_argument("path", help="Path to the library file to trash (absolute or relative to cwd)")

    feature_set = subparsers.add_parser("feature-set", help="Enable or disable one feature")
    feature_set.add_argument("feature", choices=list(FEATURE_NAMES), help="Feature name")
    feature_set.add_argument("state", choices=["on", "off"], help="Target state")

    deletion_mode_p = subparsers.add_parser(
        "deletion-mode",
        help="Show or set how a hand-deleted document is recorded in the catalog",
    )
    deletion_mode_p.add_argument(
        "mode", nargs="?", choices=list(DELETION_MODES),
        help="tombstone (default: keep id+hash+date, recognise re-deposits) or "
             "purge (keep nothing). Omit to show the current mode.",
    )

    language_p = subparsers.add_parser(
        "language",
        help="Show or set your primary language (search works in it + English)",
    )
    language_p.add_argument(
        "code", nargs="?",
        help="language code (e.g. fr, en, es). Omit to show the current one.",
    )

    subparsers.add_parser(
        "setup",
        help="Guided first run: choose where your files live (Inbox/Library/optional Mirror), then who you are",
    )
    subparsers.add_parser(
        "setup-context",
        help="Guided questionnaire to build your context file (helps the AI file your documents)",
    )

    search_p = subparsers.add_parser(
        "search",
        help="Search your library by content (offline, over the catalog fiche)",
    )
    search_p.add_argument("query", nargs="+", help="search terms")
    search_p.add_argument("--limit", type=int, default=20, help="max results (default: 20)")

    search_ai_p = subparsers.add_parser(
        "search-ai",
        help="Deeper search: an AI broadens your query with synonyms + translations (EN + your language), then searches",
    )
    search_ai_p.add_argument("query", nargs="+", help="search terms (a word or a phrase)")
    search_ai_p.add_argument("--limit", type=int, default=20, help="max results (default: 20)")

    subparsers.add_parser(
        "reindex",
        help="Build/refresh the persistent content index so search never re-reads files (no AI)",
    )

    enrich_p = subparsers.add_parser(
        "enrich-keywords",
        help="One-time: add existing documents' keywords in English + your language (uses AI)",
    )
    enrich_p.add_argument(
        "--force", action="store_true",
        help="re-process every document, even those already enriched (e.g. to refresh with a better model)",
    )

    subparsers.add_parser(
        "rescan",
        help="Follow hand reorganization of the library (moves/renames/deletes) into the catalog. No AI.",
    )

    deleted_history = subparsers.add_parser(
        "deleted-history",
        help="List library files deleted by hand (from the action log)",
    )
    deleted_history.add_argument("--limit", type=int, default=50, help="Most recent N entries (default: 50)")

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
    print(f"- search_index_file: {paths.search_index_file}")
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
    print("Deletion")
    print(f"- deletion_mode: {get_deletion_mode(paths)}")
    print("Search")
    print(f"- language: {get_user_language(paths)}")
    return 0


def cmd_deletion_mode(mode: str | None) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    if mode is None:
        print(f"deletion_mode: {get_deletion_mode(paths)}")
        return 0
    set_deletion_mode(paths, mode)
    print(f"deletion_mode set to: {mode}")
    return 0


def cmd_language(code: str | None) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    if code is None:
        print(f"language: {get_user_language(paths)}")
        return 0
    try:
        set_user_language(paths, code)
    except ValueError as err:
        print(str(err))
        return 2
    print(f"language set to: {code.strip().lower()}")
    return 0


def cmd_init_layout() -> int:
    paths = default_runtime_paths()
    mirror_enabled = bool(load_feature_settings(paths)["features"].get("mirror_sync", True))
    ensure_runtime_layout(paths, include_mirror=mirror_enabled)

    print("Runtime layout initialized:")
    print(f"- {paths.workspace_root}")
    print(f"- {paths.inbox_dir}")
    print(f"- {paths.queue_dir}")
    print(f"- {paths.inbox_trash_manual_dir}")
    print(f"- {paths.library_root}")
    print(f"- {paths.library_trash_manual_dir}")
    if mirror_enabled:
        print(f"- {paths.mirror_root}")
        print(f"- {paths.mirror_trash_dir}")
    print(f"- {paths.state_root}")
    print(f"- {paths.actions_log_file}")
    print(f"- {paths.catalog_db_file}")
    print(f"- {paths.catalog_snapshot_file}")
    print(f"- {paths.settings_file}")
    return 0


def cmd_setup() -> int:
    from procrafiler.user_setup import setup

    return setup()


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


def _live(message: str) -> None:
    """Print a pipeline progress line in real time (so the user can watch/interrupt)."""
    print(message, flush=True)


def cmd_process_once(dry_run: bool = False) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    now_utc = _resolve_now_utc()
    try:
        with runtime_lock(paths):
            reconcile_catalog_snapshot(paths, now_utc=now_utc)
            status = process_next_inbox_file(paths, now_utc=now_utc, dry_run=dry_run, progress=_live)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(f"Pipeline result: {status}")
    return 0


def cmd_process_all(dry_run: bool = False) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    now_utc = _resolve_now_utc()
    try:
        with runtime_lock(paths):
            reconcile_catalog_snapshot(paths, now_utc=now_utc)
            summary = process_all_inbox_files(paths, now_utc=now_utc, dry_run=dry_run, progress=_live)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(
        "Batch result: "
        f"processed: {summary['processed']}, "
        f"duplicates: {summary['duplicates']}, "
        f"manual_reviews: {summary['manual_reviews']}, "
        f"pending_decisions: {summary['pending_decisions']}, "
        f"organized: {summary.get('organized', 0)}, "
        f"errors: {summary['errors']}, "
        f"mirror_failures: {summary['mirror_failures']}"
    )
    if summary["pending_decisions"]:
        print(f"  {summary['pending_decisions']} file(s) awaiting your decision — run: procrafiler review")
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


def cmd_doctor() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    checks = run_doctor(paths)
    print(format_report(checks))
    return overall_exit_code(checks)


def cmd_library_trash(path_str: str) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    now_utc = _resolve_now_utc()

    target = Path(path_str).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target

    try:
        with runtime_lock(paths):
            reconcile_catalog_snapshot(paths, now_utc=now_utc)
            try:
                final_state = move_library_file_to_trash(paths, target, now_utc=now_utc)
            except LibraryTrashError as err:
                print(str(err), file=sys.stderr)
                return 1
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL

    print(f"Library trash result: {final_state}")
    return 0


def cmd_reconcile_snapshot() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    now_utc = _resolve_now_utc()
    try:
        with runtime_lock(paths):
            result = reconcile_catalog_snapshot(paths, now_utc=now_utc)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL

    if result.rewrote_snapshot:
        print(
            f"Snapshot regenerated from DB ({result.reason}): "
            f"{result.documents_in_db} documents in DB, "
            f"{result.documents_in_snapshot_before} in previous snapshot"
        )
    else:
        print(
            f"Snapshot already consistent ({result.reason}): "
            f"{result.documents_in_db} documents in DB"
        )
    return 0


def cmd_review() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    now_utc = _resolve_now_utc()
    try:
        with runtime_lock(paths):
            reconcile_catalog_snapshot(paths, now_utc=now_utc)
            # Resolve input/print at call time so the interactive loop reads the
            # real terminal (and tests can patch builtins.input).
            summary = run_review(paths, input_fn=input, output_fn=print, now_utc=now_utc)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(
        f"Review done: resolved {summary['resolved']}, "
        f"skipped {summary['skipped']} of {summary['pending']} pending."
    )
    return 0


def cmd_setup_context() -> int:
    # Resolve input/print at call time so the questionnaire reads the real
    # terminal (and tests can inject their own ask/out).
    from procrafiler.user_context_setup import setup_context

    setup_context(ask=input, out=print)
    return 0


def _print_hits(hits: list, paths: "object", *, header: str) -> None:
    print(header)
    for hit in hits:
        try:
            location = str(Path(hit.path).relative_to(paths.library_root))
        except ValueError:
            location = hit.path
        line = f"\n• {hit.name}"
        if hit.category_path:
            line += f"   [{hit.category_path}]"
        if hit.date:
            line += f"   {hit.date}"
        print(line)
        if hit.snippet:
            print(f"  {hit.snippet}")
        print(f"  {location}")


def cmd_search(terms: list[str], limit: int) -> int:
    from procrafiler.search import search_catalog

    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    query = " ".join(terms)
    hits = search_catalog(
        paths.catalog_db_file, query, limit=limit, index_path=paths.search_index_file,
        user_language=get_user_language(paths),
    )
    if not hits:
        print(f"No results for: {query}")
        return 0
    _print_hits(hits, paths, header=f"{len(hits)} result(s) for: {query}")
    return 0


def cmd_search_ai(terms: list[str], limit: int) -> int:
    from procrafiler.ai_analysis import expand_query
    from procrafiler.search import search_catalog_any

    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    query = " ".join(terms)
    language = get_user_language(paths)
    expansion = expand_query(query, language=language)
    if not expansion:
        print("AI expansion unavailable (no AI chain configured, or the call failed) — using plain search.")
        return cmd_search(terms, limit)
    print(f"Expanding “{query}” with AI: {', '.join(expansion)}")
    all_terms = list(dict.fromkeys([*query.split(), *expansion]))
    hits = search_catalog_any(
        paths.catalog_db_file, all_terms, limit=limit, index_path=paths.search_index_file,
        user_language=language,
    )
    if not hits:
        print(f"No results for: {query} (AI-expanded)")
        return 0
    _print_hits(hits, paths, header=f"{len(hits)} result(s) for: {query} (AI-expanded)")
    return 0


def cmd_reindex() -> int:
    from procrafiler.search import reindex_content

    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    counts = reindex_content(paths.catalog_db_file, index_path=paths.search_index_file)
    print(
        "Reindex content: "
        f"indexed: {counts['indexed']}, added: {counts['added']}, pruned: {counts['pruned']}"
    )
    return 0


def cmd_enrich_keywords(force: bool) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    now_utc = _resolve_now_utc()
    try:
        with runtime_lock(paths):
            counts = enrich_keywords(paths, force=force, now_utc=now_utc, emit=_live)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(
        "Enrich keywords: "
        f"enriched: {counts['enriched']}, skipped: {counts['skipped']}, failed: {counts['failed']}"
    )
    return 0


def cmd_rescan() -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    now_utc = _resolve_now_utc()
    try:
        with runtime_lock(paths):
            counts = run_rescan(paths, now_utc=now_utc, features=load_feature_settings(paths)["features"], emit=_live)
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(
        "Rescan: "
        f"moved: {counts['moved']}, re-added: {counts['readded']}, "
        f"duplicates: {counts['duplicates']}, deleted: {counts['deleted']}, "
        f"new ingested: {counts['new']}, repo docs indexed: {counts['indexed']}, "
        f"names synced: {counts['renamed']}"
    )
    return 0


def cmd_scrub(limit: int | None, no_mirror: bool, repair: bool) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    catalog = CatalogRepository(paths.catalog_db_file)
    catalog.init_schema()
    try:
        with runtime_lock(paths):
            report = run_scrub(
                paths,
                catalog,
                limit=limit,
                check_mirror=not no_mirror,
                repair=repair,
                now_utc=_resolve_now_utc().isoformat(),
            )
    except RuntimeLockedError as err:
        _print_lock_busy(err)
        return EXIT_TEMPFAIL
    print(format_scrub_report(report))
    return 0 if report.healthy else 1


def cmd_deleted_history(limit: int) -> int:
    paths = default_runtime_paths()
    ensure_runtime_layout(paths)
    log_file = paths.actions_log_file
    if not log_file.exists():
        print("No action log yet.")
        return 0
    entries: list[dict[str, object]] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "library_file_deleted":
            entries.append(event)
    if not entries:
        print("No library files have been deleted by hand.")
        return 0
    shown = entries[-limit:] if limit and limit > 0 else entries
    print(f"Library files deleted by hand ({len(shown)} of {len(entries)}):")
    for event in shown:
        print(f"- {event.get('event_time_utc', '?')}  {event.get('path_before', '?')}")
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
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "feature-set":
        return cmd_feature_set(args.feature, args.state)
    if args.command == "deletion-mode":
        return cmd_deletion_mode(args.mode)
    if args.command == "language":
        return cmd_language(args.code)
    if args.command == "process-once":
        return cmd_process_once(args.dry_run)
    if args.command == "process-all":
        return cmd_process_all(args.dry_run)
    if args.command == "purge-mirror-trash":
        return cmd_purge_mirror_trash(args.days)
    if args.command == "library-trash":
        return cmd_library_trash(args.path)
    if args.command == "reconcile-snapshot":
        return cmd_reconcile_snapshot()
    if args.command == "review":
        return cmd_review()
    if args.command == "setup":
        return cmd_setup()
    if args.command == "setup-context":
        return cmd_setup_context()
    if args.command == "search":
        return cmd_search(args.query, args.limit)
    if args.command == "search-ai":
        return cmd_search_ai(args.query, args.limit)
    if args.command == "reindex":
        return cmd_reindex()
    if args.command == "enrich-keywords":
        return cmd_enrich_keywords(args.force)
    if args.command == "rescan":
        return cmd_rescan()
    if args.command == "scrub":
        return cmd_scrub(args.limit, args.no_mirror, args.repair)
    if args.command == "deleted-history":
        return cmd_deleted_history(args.limit)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
