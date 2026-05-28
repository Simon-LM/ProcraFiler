# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from shutil import move
from typing import Any
from uuid import uuid4

from procrafiler.catalog import CatalogRepository
from procrafiler.config import RuntimePaths, ensure_runtime_layout, load_feature_settings
from procrafiler.ai_naming import suggest_stem_with_ai  # type: ignore[reportMissingImports]
from procrafiler.flow import INITIAL_STATE, validate_transition
from procrafiler.mirror import sync_library_file_to_mirror  # type: ignore[reportMissingImports]
from procrafiler.naming import build_timestamped_filename
from procrafiler.taxonomy import decide_route_for_filename  # type: ignore[reportMissingImports]


def _utc_iso(now_utc: datetime | None = None) -> str:
    dt = now_utc or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem}__{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _append_action_log(
    paths: RuntimePaths,
    *,
    operation_id: str,
    action: str,
    status: str,
    message: str,
    now_utc: datetime | None = None,
    path_before: str | None = None,
    path_after: str | None = None,
    extra_fields: dict[str, Any] | None = None,
    features: dict[str, bool] | None = None,
) -> None:
    if features is not None and not features.get("actions_log", True):
        return
    event: dict[str, Any] = {
        "event_id": str(uuid4()),
        "event_time_utc": _utc_iso(now_utc),
        "operation_id": operation_id,
        "action": action,
        "status": status,
        "message": message,
        "path_before": path_before,
        "path_after": path_after,
    }
    if extra_fields:
        event.update(extra_fields)
    with paths.actions_log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=True) + "\n")


def _write_catalog_snapshot(
    paths: RuntimePaths,
    repo: CatalogRepository,
    now_utc: datetime | None = None,
    *,
    features: dict[str, bool] | None = None,
) -> None:
    if features is not None and not features.get("catalog_snapshot", True):
        return
    documents = repo.list_documents()
    latest = documents[0]["updated_at_utc"] if documents else None
    snapshot: dict[str, Any] = {
        "meta": {
            "schema_version": "1.0",
            "generated_at_utc": _utc_iso(now_utc),
            "source_db": str(paths.catalog_db_file),
            "documents_count": len(documents),
            "last_update_utc": latest,
        },
        "documents": documents,
    }
    tmp_path = paths.catalog_snapshot_file.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(paths.catalog_snapshot_file)


def _sync_to_mirror(
    paths: RuntimePaths,
    *,
    operation_id: str,
    library_file: Path,
    now_utc: datetime | None = None,
    features: dict[str, bool] | None = None,
) -> bool:
    if features is not None and not features.get("mirror_sync", True):
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="mirror_sync_skipped",
            status="success",
            message="Mirror sync skipped: feature disabled",
            now_utc=now_utc,
            path_before=str(library_file),
            features=features,
        )
        return False

    relative_path = library_file.relative_to(paths.library_root)
    mirror_target = paths.mirror_root / relative_path

    _append_action_log(
        paths,
        operation_id=operation_id,
        action="mirror_sync_attempt",
        status="success",
        message="Mirror sync started",
        now_utc=now_utc,
        path_before=str(library_file),
        path_after=str(mirror_target),
        features=features,
    )

    result: Any = sync_library_file_to_mirror(paths, library_file, now_utc=now_utc)

    if result.quarantined_path is not None:
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="mirror_quarantine_old_version",
            status="success",
            message="Previous mirror version moved to Mirror_Trash",
            now_utc=now_utc,
            path_before=str(mirror_target),
            path_after=str(result.quarantined_path),
            features=features,
        )

    if result.success:
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="mirror_sync_success",
            status="success",
            message="Mirror sync hash verification passed",
            now_utc=now_utc,
            path_before=str(library_file),
            path_after=str(mirror_target),
            features=features,
        )
        return True

    _append_action_log(
        paths,
        operation_id=operation_id,
        action="mirror_sync_failed",
        status="failed",
        message=f"Mirror sync failed: {result.error}",
        now_utc=now_utc,
        path_before=str(library_file),
        path_after=str(mirror_target),
        features=features,
    )
    return False


def process_next_inbox_file(paths: RuntimePaths, now_utc: datetime | None = None, dry_run: bool = False) -> str:
    """Process one file from Inbox according to MVP flow rules.

    Walks the state machine declared in `procrafiler.flow`. Every transition
    goes through `validate_transition`, which raises `InvalidTransition` if
    the code ever attempts an illegal jump. The final state lands in the
    catalog's `flow_state` column for the documents we persist (manual review
    and library store paths). Duplicate paths produce no DB row, only log
    events — the spec says we never permanently delete in inbox/library.
    """
    ensure_runtime_layout(paths)
    features = load_feature_settings(paths)["features"]

    candidates = sorted([p for p in paths.inbox_dir.iterdir() if p.is_file()])
    if not candidates:
        return "NOOP"

    operation_id = str(uuid4())
    source = candidates[0]
    current_state = INITIAL_STATE

    _append_action_log(
        paths,
        operation_id=operation_id,
        action="ingest_detected",
        status="success",
        message="File detected in inbox",
        now_utc=now_utc,
        path_before=str(source),
        features=features,
    )

    queued_target = _ensure_unique_path(paths.queue_dir / source.name)
    analysis_path = source
    if not dry_run:
        move(str(source), str(queued_target))

        _append_action_log(
            paths,
            operation_id=operation_id,
            action="move_to_queue",
            status="success",
            message="File moved from inbox to queue",
            now_utc=now_utc,
            path_before=str(source),
            path_after=str(queued_target),
            features=features,
        )
        analysis_path = queued_target
    current_state = validate_transition(current_state, "INBOX_QUEUED")

    current_state = validate_transition(current_state, "PROCESSING_LOCKED")
    sha256 = _file_sha256(analysis_path)
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    current_state = validate_transition(current_state, "ANALYSIS_RUNNING")

    if dry_run:
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="dry_run_analysis",
            status="success",
            message="Dry-run analysis completed",
            now_utc=now_utc,
            path_before=str(source),
            extra_fields={"dry_run": True},
            features=features,
        )
        if repo.has_sha256(sha256):
            current_state = validate_transition(current_state, "DUPLICATE_CANDIDATE")
            current_state = validate_transition(current_state, "INBOX_TRASH_PENDING_MANUAL")
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="dry_run_duplicate_detected_exact",
                status="success",
                message="Dry-run detected duplicate by sha256",
                now_utc=now_utc,
                path_before=str(analysis_path),
                extra_fields={"dry_run": True},
                features=features,
            )
            return current_state

        current_state = validate_transition(current_state, "CLASSIFICATION_READY")
        current_state = validate_transition(current_state, "ROUTE_PROPOSED")

        route = decide_route_for_filename(source.name)
        if route.needs_manual_review:
            current_state = validate_transition(current_state, "USER_CONFIRMATION_REQUIRED")
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="dry_run_manual_review_required",
                status="warning",
                message="Dry-run requires manual review for this file",
                now_utc=now_utc,
                path_before=str(source),
                extra_fields={
                    "dry_run": True,
                    "reason": route.reason,
                    "matched_extension": route.matched_extension,
                },
                features=features,
            )
            return current_state

        current_state = validate_transition(current_state, "ROUTE_CONFIRMED")
        current_state = validate_transition(current_state, "LIBRARY_STORED")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="dry_run_route_to_library",
            status="success",
            message="Dry-run routed file to library",
            now_utc=now_utc,
            path_before=str(analysis_path),
            extra_fields={
                "dry_run": True,
                    "target_route": "/".join(route.relative_dir or ("Revue_Manuelle",)),
                "matched_extension": route.matched_extension,
            },
            features=features,
        )
        return current_state

    if repo.has_sha256(sha256):
        current_state = validate_transition(current_state, "DUPLICATE_CANDIDATE")
        trash_target = _ensure_unique_path(paths.inbox_trash_manual_dir / queued_target.name)
        move(str(queued_target), str(trash_target))
        current_state = validate_transition(current_state, "INBOX_TRASH_PENDING_MANUAL")

        _append_action_log(
            paths,
            operation_id=operation_id,
            action="duplicate_detected_exact",
            status="success",
            message="Exact duplicate detected by sha256",
            now_utc=now_utc,
            path_before=str(queued_target),
            features=features,
        )
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="move_to_inbox_trash_manual",
            status="success",
            message="Duplicate moved to manual inbox trash",
            now_utc=now_utc,
            path_before=str(queued_target),
            path_after=str(trash_target),
            features=features,
        )
        _write_catalog_snapshot(paths, repo, now_utc, features=features)
        return current_state

    current_state = validate_transition(current_state, "CLASSIFICATION_READY")
    current_state = validate_transition(current_state, "ROUTE_PROPOSED")

    route = decide_route_for_filename(queued_target.name)
    if route.needs_manual_review:
        current_state = validate_transition(current_state, "USER_CONFIRMATION_REQUIRED")
        now_iso = _utc_iso(now_utc)
        repo.upsert_document(
            doc_id=str(uuid4()),
            sha256=sha256,
            current_filename=queued_target.name,
            current_path=str(queued_target),
            status="USER_CONFIRMATION_REQUIRED",
            updated_at_utc=now_iso,
            flow_state=current_state,
        )
        _write_catalog_snapshot(paths, repo, now_utc, features=features)

        _append_action_log(
            paths,
            operation_id=operation_id,
            action="manual_review_required",
            status="warning",
            message="Manual review required for unsupported or missing extension",
            now_utc=now_utc,
            path_before=str(queued_target),
            extra_fields={
                "reason": route.reason,
                "matched_extension": route.matched_extension,
            },
            features=features,
        )
        return current_state

    current_state = validate_transition(current_state, "ROUTE_CONFIRMED")

    route_dir = route.relative_dir or ("Revue_Manuelle",)
    target_dir = paths.library_root / Path(*route_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    name_suggestion = suggest_stem_with_ai(queued_target.name)
    if name_suggestion.used_fallback:
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="ai_naming_fallback",
            status="warning",
            message="AI naming unavailable, using deterministic fallback stem",
            now_utc=now_utc,
            path_before=str(queued_target),
            extra_fields={
                "reason": name_suggestion.reason,
                "provider": name_suggestion.provider,
                "model": name_suggestion.model,
            },
            features=features,
        )
    else:
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="ai_naming_success",
            status="success",
            message="AI naming suggestion applied",
            now_utc=now_utc,
            path_before=str(queued_target),
            extra_fields={
                "provider": name_suggestion.provider,
                "model": name_suggestion.model,
                "suggested_stem": name_suggestion.stem,
            },
            features=features,
        )

    candidate_name = f"{name_suggestion.stem}{queued_target.suffix}"
    final_name = build_timestamped_filename(candidate_name, now_utc=now_utc)
    library_target = _ensure_unique_path(target_dir / final_name)
    move(str(queued_target), str(library_target))
    current_state = validate_transition(current_state, "LIBRARY_STORED")

    now_iso = _utc_iso(now_utc)
    repo.upsert_document(
        doc_id=str(uuid4()),
        sha256=sha256,
        current_filename=library_target.name,
        current_path=str(library_target),
        status="LIBRARY_STORED",
        updated_at_utc=now_iso,
        flow_state=current_state,
    )
    _write_catalog_snapshot(paths, repo, now_utc, features=features)

    _append_action_log(
        paths,
        operation_id=operation_id,
        action="move_to_library",
        status="success",
        message="File moved to library",
        now_utc=now_utc,
        path_before=str(queued_target),
        path_after=str(library_target),
        extra_fields={
            "target_route": "/".join(route_dir),
            "matched_extension": route.matched_extension,
        },
        features=features,
    )

    _sync_to_mirror(
        paths,
        operation_id=operation_id,
        library_file=library_target,
        now_utc=now_utc,
        features=features,
    )

    return current_state


class LibraryTrashError(RuntimeError):
    """Raised when a library-trash operation is rejected up-front (bad path,
    missing source, unknown to catalog).

    Transition rejections (file is in catalog but current state can't reach
    LIBRARY_TRASHED) are signalled by the standard InvalidTransition raised
    from validate_transition. The CLI catches both.
    """


def move_library_file_to_trash(
    paths: RuntimePaths,
    library_file: Path,
    *,
    now_utc: datetime | None = None,
) -> str:
    """Move a library file to Library_Trash_Manual and quarantine its mirror.

    The path must be inside library_root; we explicitly refuse to touch
    inbox, queue, or mirror trees through this command. The file must
    already exist in the catalog — bare files dropped into the library by
    hand are out of scope (the user can `process-once` them first, or
    delete them manually).

    Mirror handling: if a mirror copy exists, it is moved to mirror_trash_dir
    so the mirror stays consistent with the library. Missing mirror copies
    are tolerated (just logged).
    """
    ensure_runtime_layout(paths)
    features = load_feature_settings(paths)["features"]

    resolved = library_file.resolve()
    try:
        relative_path = resolved.relative_to(paths.library_root.resolve())
    except ValueError as exc:
        raise LibraryTrashError(
            f"refusing to trash: {library_file} is not under library_root ({paths.library_root})"
        ) from exc

    if not resolved.exists() or not resolved.is_file():
        raise LibraryTrashError(f"refusing to trash: source file missing or not a file: {library_file}")

    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    record = repo.find_by_current_path(str(resolved))
    if record is None:
        raise LibraryTrashError(
            f"refusing to trash: no catalog entry for {library_file}. "
            "Run `procrafiler process-once` first or delete the file manually."
        )

    operation_id = str(uuid4())
    current_state = record["flow_state"] or "LIBRARY_STORED"
    new_state = validate_transition(current_state, "LIBRARY_TRASHED")

    trash_target = _ensure_unique_path(paths.library_trash_manual_dir / relative_path)
    trash_target.parent.mkdir(parents=True, exist_ok=True)
    move(str(resolved), str(trash_target))

    _append_action_log(
        paths,
        operation_id=operation_id,
        action="library_trash",
        status="success",
        message="Library file moved to Library_Trash_Manual",
        now_utc=now_utc,
        path_before=str(resolved),
        path_after=str(trash_target),
        extra_fields={"previous_flow_state": current_state},
        features=features,
    )

    mirror_source = paths.mirror_root / relative_path
    if mirror_source.exists() and mirror_source.is_file():
        mirror_trash_target = _ensure_unique_path(paths.mirror_trash_dir / relative_path)
        mirror_trash_target.parent.mkdir(parents=True, exist_ok=True)
        move(str(mirror_source), str(mirror_trash_target))
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="library_trash_mirror_quarantined",
            status="success",
            message="Mirror copy quarantined to keep mirror consistent with library",
            now_utc=now_utc,
            path_before=str(mirror_source),
            path_after=str(mirror_trash_target),
            features=features,
        )
    else:
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="library_trash_mirror_absent",
            status="warning",
            message="No mirror copy found to quarantine — mirror may have been out of sync",
            now_utc=now_utc,
            path_before=str(mirror_source),
            features=features,
        )

    repo.upsert_document(
        doc_id=str(record["doc_id"]),
        sha256=str(record["sha256"]),
        current_filename=trash_target.name,
        current_path=str(trash_target),
        status=new_state,
        updated_at_utc=_utc_iso(now_utc),
        flow_state=new_state,
    )
    _write_catalog_snapshot(paths, repo, now_utc, features=features)

    return new_state


def process_all_inbox_files(paths: RuntimePaths, now_utc: datetime | None = None, dry_run: bool = False) -> dict[str, int]:
    ensure_runtime_layout(paths)
    features = load_feature_settings(paths)["features"]

    summary = {
        "processed": 0,
        "duplicates": 0,
        "manual_reviews": 0,
        "errors": 0,
        "mirror_failures": 0,
        "total": 0,
    }

    if dry_run:
        repo = CatalogRepository(paths.catalog_db_file)
        repo.init_schema()
        known_hashes = {doc["sha256"] for doc in repo.list_documents()}
        for candidate in sorted([p for p in paths.inbox_dir.iterdir() if p.is_file()]):
            sha256 = _file_sha256(candidate)
            summary["total"] += 1
            if sha256 in known_hashes:
                summary["duplicates"] += 1
            else:
                route = decide_route_for_filename(candidate.name)
                if route.needs_manual_review:
                    summary["manual_reviews"] += 1
                    known_hashes.add(sha256)
                else:
                    summary["processed"] += 1
                    known_hashes.add(sha256)

        _append_action_log(
            paths,
            operation_id=str(uuid4()),
            action="process_all_summary",
            status="success",
            message=(
                "Batch completed: "
                f"processed={summary['processed']}, duplicates={summary['duplicates']}, "
                f"manual_reviews={summary['manual_reviews']}, errors={summary['errors']}, "
                f"mirror_failures={summary['mirror_failures']}"
            ),
            now_utc=now_utc,
            extra_fields={"dry_run": True},
            features=features,
        )
        return summary

    while True:
        previous_lines = [
            line
            for line in paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        previous_count = len(previous_lines)

        status = process_next_inbox_file(paths, now_utc=now_utc, dry_run=False)
        if status == "NOOP":
            break

        summary["total"] += 1
        if status == "LIBRARY_STORED":
            summary["processed"] += 1
        elif status == "INBOX_TRASH_PENDING_MANUAL":
            summary["duplicates"] += 1
        elif status == "USER_CONFIRMATION_REQUIRED":
            summary["manual_reviews"] += 1
        elif status.startswith("ERROR"):
            summary["errors"] += 1

        new_lines = [
            line
            for line in paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ][previous_count:]
        for line in new_lines:
            event = json.loads(line)
            if event.get("action") == "mirror_sync_failed":
                summary["mirror_failures"] += 1

    _append_action_log(
        paths,
        operation_id=str(uuid4()),
        action="process_all_summary",
        status="success",
        message=(
            "Batch completed: "
            f"processed={summary['processed']}, duplicates={summary['duplicates']}, "
            f"manual_reviews={summary['manual_reviews']}, errors={summary['errors']}, "
            f"mirror_failures={summary['mirror_failures']}"
        ),
        now_utc=now_utc,
        extra_fields={"dry_run": False},
        features=features,
    )

    return summary
