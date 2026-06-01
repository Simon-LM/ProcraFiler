# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import move
from typing import Any
from uuid import uuid4

# Optional live-progress callback: the CLI passes one to stream human-readable
# lines as each file is processed (so the user can watch and interrupt). The
# pipeline never prints directly — it only calls this when given.
ProgressFn = Callable[[str], None]

from procrafiler.catalog import CatalogRepository
from procrafiler.config import RuntimePaths, ensure_runtime_layout, load_feature_settings, load_runtime_policy
from procrafiler.ai_classification import classify_content  # type: ignore[reportMissingImports]
from procrafiler.ai_naming import suggest_stem_with_ai  # type: ignore[reportMissingImports]
from procrafiler.ai_reader import read_with_ocr, read_with_vision  # type: ignore[reportMissingImports]
from procrafiler.content_reader import extract_text_content
from procrafiler.flow import INITIAL_STATE, validate_transition
from procrafiler.mirror import sync_library_file_to_mirror  # type: ignore[reportMissingImports]
from procrafiler.naming import build_timestamped_filename
from procrafiler.taxonomy import (  # type: ignore[reportMissingImports]
    INTERIM_LIBRARY_DIR,
    category_label,
    classifiable_categories,
    dispatch_for_filename,
    existing_category_paths,
    normalize_category_path,
)


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


def _iter_inbox_files(inbox_dir: Path) -> list[Path]:
    """All files anywhere under the Inbox, recursively — but NEVER outside it.

    Files dropped inside subfolders must be processed too (the AI re-classifies
    them, so the original folder structure is irrelevant). Two safety rules keep
    the scan strictly inside the Inbox:

    - `os.walk(..., followlinks=False)` does not descend into symlinked
      directories, so a symlinked folder can't drag the scan elsewhere.
    - every candidate's resolved path must be under the resolved Inbox; anything
      that escapes (e.g. a symlinked file pointing outside) is skipped.
    """
    inbox_root = inbox_dir.resolve()
    files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(inbox_dir, followlinks=False):
        for name in filenames:
            candidate = Path(dirpath) / name
            if not candidate.is_file():
                continue
            try:
                candidate.resolve().relative_to(inbox_root)
            except (OSError, ValueError):
                # Resolves outside the Inbox (symlink escape) or is unreadable.
                continue
            files.append(candidate)
    return sorted(files)


def _resolve_document_date(ai_date: str | None, source_path: Path, now_utc: datetime | None) -> datetime:
    """Pick the date used to prefix the stored filename.

    Cascade: the date the AI found inside the document content (at midnight UTC —
    a document states a day, not a time, and midnight keeps same-day files
    grouped instead of scattered by processing seconds) → the file's
    modification time → the processing time. This only affects the FILENAME
    prefix; action-log and catalog timestamps keep the real processing time.
    """
    if ai_date:
        try:
            return datetime.strptime(ai_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        pass
    return now_utc or datetime.now(timezone.utc)


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


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of comparing catalog_snapshot.json against the SQLite catalog.

    `reason` explains what happened:
    - "consistent": snapshot already matched the DB; nothing was written.
    - "missing": snapshot file did not exist or was empty.
    - "unreadable": snapshot file existed but failed to parse as JSON.
    - "content_mismatch": snapshot existed but its doc set or updated_at_utc
       differed from the DB.
    - "feature_disabled": catalog_snapshot feature flag is off; nothing done.
    """

    reason: str
    documents_in_db: int
    documents_in_snapshot_before: int | None
    rewrote_snapshot: bool


def reconcile_catalog_snapshot(
    paths: RuntimePaths,
    *,
    now_utc: datetime | None = None,
) -> ReconcileResult:
    """Compare snapshot.json against catalog.db and rewrite if out of sync.

    Implements spec §4: the snapshot must stay synchronized with SQLite and
    be repaired on startup if a mismatch is detected. The DB is the source
    of truth — the snapshot is always rewritten from the DB on mismatch,
    never the other way around.
    """
    ensure_runtime_layout(paths)
    features = load_feature_settings(paths)["features"]

    if not features.get("catalog_snapshot", True):
        return ReconcileResult(
            reason="feature_disabled",
            documents_in_db=0,
            documents_in_snapshot_before=None,
            rewrote_snapshot=False,
        )

    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    db_documents = repo.list_documents()
    db_index = {str(doc["doc_id"]): doc for doc in db_documents}

    snapshot_documents: list[dict[str, Any]] | None
    snapshot_file = paths.catalog_snapshot_file
    if not snapshot_file.exists() or snapshot_file.stat().st_size == 0:
        snapshot_documents = None
        reason = "missing"
    else:
        try:
            parsed = json.loads(snapshot_file.read_text(encoding="utf-8"))
            raw_docs = parsed.get("documents", []) if isinstance(parsed, dict) else []
            snapshot_documents = list(raw_docs) if isinstance(raw_docs, list) else []
            reason = "consistent"
        except (json.JSONDecodeError, OSError):
            snapshot_documents = None
            reason = "unreadable"

    if snapshot_documents is not None and reason == "consistent":
        snap_index = {str(doc.get("doc_id")): doc for doc in snapshot_documents}
        if set(snap_index.keys()) != set(db_index.keys()):
            reason = "content_mismatch"
        else:
            for doc_id, db_doc in db_index.items():
                if snap_index[doc_id].get("updated_at_utc") != db_doc.get("updated_at_utc"):
                    reason = "content_mismatch"
                    break

    rewrote = False
    if reason != "consistent":
        _write_catalog_snapshot(paths, repo, now_utc, features=features)
        rewrote = True
        _append_action_log(
            paths,
            operation_id=str(uuid4()),
            action="snapshot_reconciled",
            status="success",
            message=f"Snapshot regenerated from DB ({reason})",
            now_utc=now_utc,
            extra_fields={
                "reason": reason,
                "documents_in_db": len(db_documents),
            },
            features=features,
        )

    return ReconcileResult(
        reason=reason,
        documents_in_db=len(db_documents),
        documents_in_snapshot_before=len(snapshot_documents) if snapshot_documents is not None else None,
        rewrote_snapshot=rewrote,
    )


MIRROR_SYNCED = "synced"
MIRROR_SKIPPED = "skipped"
MIRROR_FAILED = "failed"


def _sync_to_mirror(
    paths: RuntimePaths,
    *,
    operation_id: str,
    library_file: Path,
    now_utc: datetime | None = None,
    features: dict[str, bool] | None = None,
) -> str:
    """Sync one library file to the mirror.

    Returns one of MIRROR_SYNCED / MIRROR_SKIPPED / MIRROR_FAILED. The caller
    distinguishes a genuine failure (hash mismatch, copy error) from a
    deliberate skip (mirror_sync feature off) — only the former should count
    as a mirror failure in batch summaries.
    """
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
        return MIRROR_SKIPPED

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
        return MIRROR_SYNCED

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
    return MIRROR_FAILED


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of processing one inbox file.

    `flow_state` is the terminal state string (same value the public
    `process_next_inbox_file` returns). `mirror_failed` is True only when a
    mirror sync was attempted and failed — never when it was skipped because
    the mirror_sync feature is off. The batch loop tallies this inline
    instead of re-reading the action log after every file.
    """

    flow_state: str
    mirror_failed: bool


def _process_next_inbox_file(
    paths: RuntimePaths,
    now_utc: datetime | None = None,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> ProcessResult:
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
    emit: ProgressFn = progress or (lambda _message: None)

    candidates = _iter_inbox_files(paths.inbox_dir)
    if not candidates:
        return ProcessResult("NOOP", mirror_failed=False)

    operation_id = str(uuid4())
    source = candidates[0]
    current_state = INITIAL_STATE
    try:
        display_name = str(source.relative_to(paths.inbox_dir))
    except ValueError:
        display_name = source.name
    emit(f"→ {display_name}")

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
            return ProcessResult(current_state, mirror_failed=False)

        current_state = validate_transition(current_state, "CLASSIFICATION_READY")
        current_state = validate_transition(current_state, "ROUTE_PROPOSED")

        dispatch = dispatch_for_filename(source.name)
        if not dispatch.can_dispatch:
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
                    "reason": dispatch.reason,
                    "matched_extension": dispatch.matched_extension,
                },
                features=features,
            )
            return ProcessResult(current_state, mirror_failed=False)

        current_state = validate_transition(current_state, "ROUTE_CONFIRMED")
        current_state = validate_transition(current_state, "LIBRARY_STORED")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="dry_run_route_to_library",
            status="success",
            message="Dry-run routed file to interim review pending AI classification",
            now_utc=now_utc,
            path_before=str(analysis_path),
            extra_fields={
                "dry_run": True,
                "target_route": "/".join(INTERIM_LIBRARY_DIR),
                "media_type": dispatch.media_type,
                "matched_extension": dispatch.matched_extension,
            },
            features=features,
        )
        return ProcessResult(current_state, mirror_failed=False)

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
        emit("   duplicate → Inbox_Trash_Manual")
        return ProcessResult(current_state, mirror_failed=False)

    current_state = validate_transition(current_state, "CLASSIFICATION_READY")
    current_state = validate_transition(current_state, "ROUTE_PROPOSED")

    dispatch = dispatch_for_filename(queued_target.name)
    if not dispatch.can_dispatch:
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
                "reason": dispatch.reason,
                "matched_extension": dispatch.matched_extension,
            },
            features=features,
        )
        emit(f"   → manual review (unreadable: {dispatch.reason})")
        return ProcessResult(current_state, mirror_failed=False)

    current_state = validate_transition(current_state, "ROUTE_CONFIRMED")

    # Read the content locally (no AI call here). For text files and readable
    # PDFs this yields the text the AI classifier consumes below; for
    # scans/images it records which AI reader is still needed (built later).
    extraction = extract_text_content(queued_target, dispatch.media_type or "")
    emit(
        f"   read: {dispatch.media_type}"
        + (
            f", {len(extraction.text)} chars"
            if extraction.text is not None
            else f" (needs {extraction.reader_hint or 'AI reader'})"
        )
    )
    _append_action_log(
        paths,
        operation_id=operation_id,
        action="content_read",
        status="success",
        message="Local content extraction completed",
        now_utc=now_utc,
        path_before=str(queued_target),
        extra_fields={
            "media_type": dispatch.media_type,
            "reason": extraction.reason,
            "text_chars": len(extraction.text) if extraction.text is not None else 0,
            "needs_ai_reader": extraction.needs_ai_reader,
            "reader_hint": extraction.reader_hint,
        },
        features=features,
    )

    # The unified content text: what we read locally, or — for scanned PDFs and
    # images — what the OCR / vision reader produces. Naming and classification
    # both consume it.
    content_text = extraction.text
    if (content_text is None or not content_text.strip()) and extraction.reader_hint == "ocr":
        ocr_result = read_with_ocr(queued_target)
        if ocr_result.text and ocr_result.text.strip():
            content_text = ocr_result.text
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="ocr_read_success",
                status="success",
                message="OCR read scanned document",
                now_utc=now_utc,
                path_before=str(queued_target),
                extra_fields={
                    "provider": ocr_result.provider,
                    "model": ocr_result.model,
                    "text_chars": len(ocr_result.text),
                },
                features=features,
            )
            emit(f"   OCR: {len(ocr_result.text)} chars")
        else:
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="ocr_read_unavailable",
                status="warning",
                message="OCR unavailable or empty, routing to manual review",
                now_utc=now_utc,
                path_before=str(queued_target),
                extra_fields={"reason": ocr_result.reason},
                features=features,
            )
            emit(f"   OCR unavailable ({ocr_result.reason})")
    elif (content_text is None or not content_text.strip()) and extraction.reader_hint == "vision":
        vision_result = read_with_vision(queued_target)
        if vision_result.text and vision_result.text.strip():
            content_text = vision_result.text
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="vision_read_success",
                status="success",
                message="Vision model read image",
                now_utc=now_utc,
                path_before=str(queued_target),
                extra_fields={
                    "provider": vision_result.provider,
                    "model": vision_result.model,
                    "text_chars": len(vision_result.text),
                },
                features=features,
            )
            emit(f"   vision: {len(vision_result.text)} chars")
        else:
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="vision_read_unavailable",
                status="warning",
                message="Vision reader unavailable or empty, routing to manual review",
                now_utc=now_utc,
                path_before=str(queued_target),
                extra_fields={"reason": vision_result.reason},
                features=features,
            )
            emit(f"   vision unavailable ({vision_result.reason})")

    # Classify from the content. The category is decided by AI from what the
    # file CONTAINS, never from its extension or original name. Files we can't
    # read at all (images awaiting their vision reader, or OCR unavailable) and
    # any uncertain/unconfigured AI outcome fall back to the interim review
    # bucket — never to a guessed category.
    route_dir = INTERIM_LIBRARY_DIR
    if content_text is not None and content_text.strip():
        base_categories = [category_label(c) for c in classifiable_categories()]
        existing_paths = existing_category_paths(paths.library_root)
        classification = classify_content(
            content_text, base_categories=base_categories, existing_paths=existing_paths
        )
        max_depth = load_runtime_policy(paths).taxonomy_max_depth
        validated = normalize_category_path(classification.path, max_depth) if classification.path else None
        if validated is not None:
            route_dir = validated
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="classification_success",
                status="success",
                message="AI classified document from content",
                now_utc=now_utc,
                path_before=str(queued_target),
                extra_fields={
                    "category": "/".join(validated),
                    "proposed_path": classification.path,
                    "provider": classification.provider,
                    "model": classification.model,
                },
                features=features,
            )
            emit(f"   classified → {'/'.join(validated)}")
        else:
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="classification_manual_review",
                status="warning",
                message="AI classification unavailable or uncertain, routing to manual review",
                now_utc=now_utc,
                path_before=str(queued_target),
                extra_fields={
                    "reason": classification.reason,
                    "provider": classification.provider,
                    "model": classification.model,
                },
                features=features,
            )
            emit(f"   → manual review ({classification.reason})")

    target_dir = paths.library_root / Path(*route_dir)
    if not target_dir.exists():
        emit(f"   created folder: {'/'.join(route_dir)}")
    target_dir.mkdir(parents=True, exist_ok=True)

    # Name from the content the reader produced (local extraction or OCR), not
    # the original filename. When there is no readable content (images awaiting
    # their vision reader, or OCR unavailable), suggest_stem_with_ai falls back
    # to the filename stem as a last resort.
    naming_content = content_text if (content_text and content_text.strip()) else ""
    name_suggestion = suggest_stem_with_ai(
        naming_content,
        fallback_stem=Path(queued_target.name).stem,
    )
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
    # Date the file by its CONTENT (the date the AI found in it), falling back to
    # the file's mtime, then the processing time. Only the filename prefix uses
    # this; logs/catalog keep the real processing time (now_utc).
    document_dt = _resolve_document_date(name_suggestion.document_date, queued_target, now_utc)
    final_name = build_timestamped_filename(candidate_name, now_utc=document_dt)
    library_target = _ensure_unique_path(target_dir / final_name)
    move(str(queued_target), str(library_target))
    current_state = validate_transition(current_state, "LIBRARY_STORED")
    emit(f"   filed → {'/'.join(route_dir)}/{library_target.name}")

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
        message="File moved to interim review directory pending AI classification",
        now_utc=now_utc,
        path_before=str(queued_target),
        path_after=str(library_target),
        extra_fields={
            "target_route": "/".join(route_dir),
            "media_type": dispatch.media_type,
            "matched_extension": dispatch.matched_extension,
        },
        features=features,
    )

    mirror_status = _sync_to_mirror(
        paths,
        operation_id=operation_id,
        library_file=library_target,
        now_utc=now_utc,
        features=features,
    )

    return ProcessResult(current_state, mirror_failed=mirror_status == MIRROR_FAILED)


def process_next_inbox_file(
    paths: RuntimePaths,
    now_utc: datetime | None = None,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> str:
    """Public entry point: process one inbox file and return its terminal state.

    Thin wrapper over `_process_next_inbox_file` that preserves the historical
    string return contract. Callers needing the mirror-failure flag (the batch
    loop) call the internal helper directly. `progress`, if given, receives
    human-readable lines as the file is processed.
    """
    return _process_next_inbox_file(paths, now_utc=now_utc, dry_run=dry_run, progress=progress).flow_state


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


def process_all_inbox_files(
    paths: RuntimePaths,
    now_utc: datetime | None = None,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, int]:
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
        for candidate in _iter_inbox_files(paths.inbox_dir):
            sha256 = _file_sha256(candidate)
            summary["total"] += 1
            if sha256 in known_hashes:
                summary["duplicates"] += 1
            else:
                dispatch = dispatch_for_filename(candidate.name)
                if not dispatch.can_dispatch:
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
        result = _process_next_inbox_file(paths, now_utc=now_utc, dry_run=False, progress=progress)
        if result.flow_state == "NOOP":
            break

        summary["total"] += 1
        if result.flow_state == "LIBRARY_STORED":
            summary["processed"] += 1
        elif result.flow_state == "INBOX_TRASH_PENDING_MANUAL":
            summary["duplicates"] += 1
        elif result.flow_state == "USER_CONFIRMATION_REQUIRED":
            summary["manual_reviews"] += 1
        elif result.flow_state.startswith("ERROR"):
            summary["errors"] += 1

        if result.mirror_failed:
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
