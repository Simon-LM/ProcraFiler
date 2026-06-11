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
from procrafiler.ai_analysis import analyze_content  # type: ignore[reportMissingImports]
from procrafiler.ai_grouping import propose_grouping  # type: ignore[reportMissingImports]
from procrafiler.ai_organize import organize_set  # type: ignore[reportMissingImports]
from procrafiler.user_context import load_user_context  # type: ignore[reportMissingImports]
from procrafiler.ai_naming import task_chain_from_env  # type: ignore[reportMissingImports]
from procrafiler.ai_reader import read_with_ocr, read_with_vision  # type: ignore[reportMissingImports]
from procrafiler.content_reader import extract_text_content
from procrafiler.flow import INITIAL_STATE, validate_transition
from procrafiler.mirror import sync_library_file_to_mirror  # type: ignore[reportMissingImports]
from procrafiler.naming import build_timestamped_filename, sanitize_filename_stem
from procrafiler.taxonomy import (  # type: ignore[reportMissingImports]
    INTERIM_LIBRARY_DIR,
    category_label,
    classifiable_categories,
    dispatch_for_filename,
    existing_category_paths,
    normalize_category_path,
    normalize_review_path,
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


def _prune_empty_inbox_dirs(inbox_dir: Path) -> int:
    """Remove now-empty SUBdirectories left under the Inbox after files move out.

    The user drops files inside arbitrary subfolders (e.g. `Inbox/CV/…`); once
    every file in such a folder has been processed, the empty folder is clutter.
    We delete those, bottom-up so nested empties go too, under strict bounds:

    - The Inbox root itself is NEVER removed (it is the drop point).
    - We never follow symlinked directories (`os.walk(followlinks=False)`), and
      never `rmdir` a symlink — only real directories whose resolved path is
      inside the Inbox. A symlink pointing elsewhere can't drag us out.
    - `rmdir` only succeeds on an already-empty directory, so a folder that still
      holds an unprocessed file (or a non-empty subfolder) is left untouched.

    Returns the number of directories removed.
    """
    inbox_root = inbox_dir.resolve()
    if not inbox_root.exists():
        return 0
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(inbox_root, topdown=False, followlinks=False):
        candidate = Path(dirpath)
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == inbox_root:
            continue  # never remove the Inbox root itself
        try:
            resolved.relative_to(inbox_root)
        except ValueError:
            continue  # outside the Inbox — refuse to touch
        try:
            candidate.rmdir()  # succeeds only if empty
            removed += 1
        except OSError:
            pass  # not empty (or vanished) — leave it
    return removed


def _exif_capture_datetime(path: Path) -> datetime | None:
    """The photo's own capture date from EXIF (DateTimeOriginal), or None.

    A hard metadata fact, and far more reliable than a vision model reading a
    date off the image (which can hallucinate). EXIF carries no timezone, so the
    naive value is treated as UTC for consistency with the rest of the pipeline.
    Any problem (no Pillow, no EXIF, unparseable) yields None — the caller then
    falls through the cascade.
    """
    try:
        from PIL import Image  # optional dep; absence just disables EXIF dating
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            raw = None
            try:
                # DateTimeOriginal (36867) lives in the Exif sub-IFD (0x8769).
                raw = exif.get_ifd(0x8769).get(36867)
            except Exception:
                raw = None
            if not isinstance(raw, str) or not raw.strip():
                raw = exif.get(306)  # DateTime (fallback)
            if not isinstance(raw, str) or not raw.strip():
                return None
            return datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _resolve_document_date(
    ai_date: str | None,
    source_path: Path,
    now_utc: datetime | None,
    *,
    media_type: str | None = None,
) -> datetime:
    """Pick the date used to prefix the stored filename.

    Cascade:
    1. For images, the EXIF capture date (DateTimeOriginal) — real metadata, and
       it sidesteps vision date-hallucination; it also makes photos taken the
       same day group naturally.
    2. else the date the AI found inside the document content (at midnight UTC —
       a document states a day, not a time, and midnight keeps same-day files
       grouped instead of scattered by processing seconds).
    3. else the file's modification time.
    4. else the processing time.
    This only affects the FILENAME prefix; action-log and catalog timestamps keep
    the real processing time.
    """
    if media_type == "image":
        exif_dt = _exif_capture_datetime(source_path)
        if exif_dt is not None:
            return exif_dt
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
    # The catalog stores the per-document fiche (§4.1) as a JSON *string* in
    # `content_json` (queryable via json_extract). Inline it as nested JSON in
    # the snapshot so the human-readable mirror shows the fiche, not an escaped
    # blob.
    for document in documents:
        raw_content = document.pop("content_json", None)
        parsed_content: Any = None
        if raw_content:
            try:
                parsed_content = json.loads(raw_content)
            except (TypeError, ValueError):
                parsed_content = None
        document["content"] = parsed_content
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
    the mirror_sync feature is off. `pending` is True when the file was parked
    in the decisions queue (the AI was unsure and offered options): it is
    physically in Manual_Review with flow_state LIBRARY_STORED, but awaits
    `review`, so the batch loop counts it separately from a settled placement.
    The batch loop tallies these inline instead of re-reading the action log
    after every file.
    """

    flow_state: str
    mirror_failed: bool
    pending: bool = False
    # The catalog doc_id when the file was stored in the library (LIBRARY_STORED,
    # not pending). The batch then hands these to the organize pass for grouping.
    doc_id: str | None = None


@dataclass
class _CatalogedDoc:
    """One Inbox file read + analyzed but NOT yet filed (it waits in the Queue).

    The catalog phase produces these; the file phase consumes them. Keeping the
    two apart lets the organize phase look at a whole FOLDER's docs together
    before deciding where each one goes (so a coherent set isn't scattered)."""

    queued_target: Path
    source: Path
    source_folder: str
    sha256: str
    dispatch: Any
    operation_id: str
    current_state: str
    content_text: str | None = None
    read_via: str | None = None
    analysis: Any = None  # AnalysisResult | None
    max_depth: int = 0


def _read_and_analyze(
    paths: RuntimePaths,
    *,
    queued_target: Path,
    source: Path,
    source_folder: str,
    sha256: str,
    dispatch: Any,
    operation_id: str,
    current_state: str,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> _CatalogedDoc:
    """Read the file's content (local / OCR / vision) and run the single AI
    analysis → a fiche. Does NOT decide a final folder or file anything; returns
    a `_CatalogedDoc` for the caller (per-file or set-aware) to place."""
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

    content_text = extraction.text
    read_via: str | None = "text" if (content_text is not None and content_text.strip()) else None
    if (content_text is None or not content_text.strip()) and extraction.reader_hint == "ocr":
        ocr_result = read_with_ocr(queued_target)
        if ocr_result.text and ocr_result.text.strip():
            content_text = ocr_result.text
            read_via = "ocr"
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
            read_via = "vision"
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

    analysis = None
    max_depth = load_runtime_policy(paths).taxonomy_max_depth
    if content_text is not None and content_text.strip():
        base_categories = [category_label(c) for c in classifiable_categories()]
        existing_paths = existing_category_paths(paths.library_root)
        analysis = analyze_content(
            content_text,
            base_categories=base_categories,
            existing_paths=existing_paths,
            original_filename=source.name,
            source_folder=source_folder or None,
            user_context=load_user_context(),
        )

    return _CatalogedDoc(
        queued_target=queued_target,
        source=source,
        source_folder=source_folder,
        sha256=sha256,
        dispatch=dispatch,
        operation_id=operation_id,
        current_state=current_state,
        content_text=content_text,
        read_via=read_via,
        analysis=analysis,
        max_depth=max_depth,
    )


def _route_from_analysis(
    paths: RuntimePaths,
    catdoc: _CatalogedDoc,
    *,
    organized_path: str | None = None,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> tuple[tuple[str, ...], list[str] | None, str | None]:
    """Decide the final folder for one cataloged doc.

    `organized_path` (set-aware organize's decision for this doc) wins when it
    validates against the taxonomy — that's how the whole-set decision overrides
    a context-blind per-file guess, so a folder's files don't scatter or leak to
    the decisions queue individually. Otherwise fall back to the per-file
    analysis: a confident category, else the decisions queue (alternatives), else
    plain manual review. Returns (route_dir, pending_options, pending_reason)."""
    analysis = catdoc.analysis
    max_depth = catdoc.max_depth

    if organized_path is not None:
        validated = normalize_category_path(organized_path, max_depth)
        if validated is not None:
            _append_action_log(
                paths,
                operation_id=catdoc.operation_id,
                action="organize_placed",
                status="success",
                message="Set-aware organize placed the document",
                now_utc=now_utc,
                path_before=str(catdoc.queued_target),
                extra_fields={"category": "/".join(validated), "proposed_path": organized_path},
                features=features,
            )
            emit(f"   organized → {'/'.join(validated)}")
            return validated, None, None

    if analysis is not None:
        validated = normalize_category_path(analysis.category_path, max_depth) if analysis.category_path else None
        if validated is not None:
            _append_action_log(
                paths,
                operation_id=catdoc.operation_id,
                action="analysis_success",
                status="success",
                message="AI analyzed and classified document from content",
                now_utc=now_utc,
                path_before=str(catdoc.queued_target),
                extra_fields={
                    "category": "/".join(validated),
                    "proposed_path": analysis.category_path,
                    "provider": analysis.provider,
                    "model": analysis.model,
                },
                features=features,
            )
            emit(f"   classified → {'/'.join(validated)}")
            return validated, None, None

        options: list[str] = []
        for alt in analysis.alternatives:
            normalized = normalize_category_path(alt, max_depth)
            if normalized is not None:
                label = "/".join(normalized)
                if label not in options:
                    options.append(label)
        if options:
            pending_reason = analysis.reason or "uncertain_with_options"
            _append_action_log(
                paths,
                operation_id=catdoc.operation_id,
                action="decision_pending",
                status="warning",
                message="AI uncertain, parking file in the decisions queue for review",
                now_utc=now_utc,
                path_before=str(catdoc.queued_target),
                extra_fields={
                    "reason": pending_reason,
                    "options": options,
                    "provider": analysis.provider,
                    "model": analysis.model,
                },
                features=features,
            )
            emit(f"   → decision pending ({len(options)} options)")
            return INTERIM_LIBRARY_DIR, options, pending_reason

        _append_action_log(
            paths,
            operation_id=catdoc.operation_id,
            action="analysis_manual_review",
            status="warning",
            message="AI analysis unavailable or uncertain, routing to manual review",
            now_utc=now_utc,
            path_before=str(catdoc.queued_target),
            extra_fields={
                "reason": analysis.reason,
                "provider": analysis.provider,
                "model": analysis.model,
            },
            features=features,
        )
        emit(f"   → manual review ({analysis.reason})")

    return INTERIM_LIBRARY_DIR, None, None


def _file_cataloged(
    paths: RuntimePaths,
    catdoc: _CatalogedDoc,
    *,
    route_dir: tuple[str, ...],
    pending_options: list[str] | None,
    pending_reason: str | None,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> ProcessResult:
    """Name, date, move the file into `route_dir`, persist its fiche, and mirror.
    The route was decided by the caller (per-file or set-aware organize)."""
    analysis = catdoc.analysis
    queued_target = catdoc.queued_target
    source = catdoc.source
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()

    target_dir = paths.library_root / Path(*route_dir)
    if not target_dir.exists():
        emit(f"   created folder: {'/'.join(route_dir)}")
    target_dir.mkdir(parents=True, exist_ok=True)

    if analysis is not None and analysis.name:
        stem = analysis.name
    else:
        stem = sanitize_filename_stem(Path(queued_target.name).stem)
    document_date = analysis.document_date if analysis is not None else None

    candidate_name = f"{stem}{queued_target.suffix}"
    document_dt = _resolve_document_date(document_date, queued_target, now_utc, media_type=catdoc.dispatch.media_type)
    final_name = build_timestamped_filename(candidate_name, now_utc=document_dt)
    library_target = _ensure_unique_path(target_dir / final_name)
    move(str(queued_target), str(library_target))
    current_state = validate_transition(catdoc.current_state, "LIBRARY_STORED")
    emit(f"   filed → {'/'.join(route_dir)}/{library_target.name}")

    now_iso = _utc_iso(now_utc)
    is_routed = tuple(route_dir) != tuple(INTERIM_LIBRARY_DIR)
    fiche: dict[str, Any] = {
        "name": analysis.name if analysis is not None else None,
        "document_date": document_date,
        "category_path": "/".join(route_dir) if is_routed else None,
        "alternatives": pending_options or (analysis.alternatives if analysis is not None else []),
        "summary": analysis.summary if analysis is not None else None,
        "keywords": analysis.keywords if analysis is not None else [],
        "entities": analysis.entities if analysis is not None else {},
        "language": analysis.language if analysis is not None else None,
        "source_folder": catdoc.source_folder or None,
        "original_filename": source.name,
        "effective_date": document_dt.strftime("%Y-%m-%d"),
        "read_via": catdoc.read_via,
        "provider": analysis.provider if analysis is not None else None,
        "model": analysis.model if analysis is not None else None,
        "analyzed_at": now_iso,
    }
    content_json = json.dumps(fiche, ensure_ascii=True)

    is_pending = bool(pending_options)
    if is_pending:
        pending_blob = json.dumps(
            {
                "options": pending_options,
                "reason": pending_reason,
                "snippet": (catdoc.content_text or "")[:280],
            }
        )
        catalog_status = "DECISION_PENDING"
    else:
        pending_blob = None
        catalog_status = "LIBRARY_STORED"

    doc_id = str(uuid4())
    repo.upsert_document(
        doc_id=doc_id,
        sha256=catdoc.sha256,
        current_filename=library_target.name,
        current_path=str(library_target),
        status=catalog_status,
        updated_at_utc=now_iso,
        flow_state=current_state,
        pending_decision=pending_blob,
        content_json=content_json,
    )
    _write_catalog_snapshot(paths, repo, now_utc, features=features)

    _append_action_log(
        paths,
        operation_id=catdoc.operation_id,
        action="move_to_library",
        status="success",
        message="File stored in the library",
        now_utc=now_utc,
        path_before=str(queued_target),
        path_after=str(library_target),
        extra_fields={
            "target_route": "/".join(route_dir),
            "media_type": catdoc.dispatch.media_type,
            "matched_extension": catdoc.dispatch.matched_extension,
        },
        features=features,
    )

    if is_pending:
        return ProcessResult(current_state, mirror_failed=False, pending=True)

    mirror_status = _sync_to_mirror(
        paths,
        operation_id=catdoc.operation_id,
        library_file=library_target,
        now_utc=now_utc,
        features=features,
    )
    return ProcessResult(current_state, mirror_failed=mirror_status == MIRROR_FAILED, doc_id=doc_id)


def _dry_run_one(
    paths: RuntimePaths,
    source: Path,
    *,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> ProcessResult:
    """Dry-run a single inbox file: walk the states and report where it WOULD
    go, without moving anything or calling the AI."""
    operation_id = str(uuid4())
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
    current_state = validate_transition(current_state, "INBOX_QUEUED")
    current_state = validate_transition(current_state, "PROCESSING_LOCKED")
    sha256 = _file_sha256(source)
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    current_state = validate_transition(current_state, "ANALYSIS_RUNNING")

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
            path_before=str(source),
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
        path_before=str(source),
        extra_fields={
            "dry_run": True,
            "target_route": "/".join(INTERIM_LIBRARY_DIR),
            "media_type": dispatch.media_type,
            "matched_extension": dispatch.matched_extension,
        },
        features=features,
    )
    return ProcessResult(current_state, mirror_failed=False)


def _catalog_one_inbox_file(
    paths: RuntimePaths,
    source: Path,
    *,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
    extra_known_hashes: frozenset[str] | set[str] = frozenset(),
) -> _CatalogedDoc | ProcessResult:
    """Phase 1 (CATALOG) for ONE file: move it to the Queue, dedup, dispatch, and
    read+analyze it into a fiche — WITHOUT filing it into the library. Returns the
    `_CatalogedDoc` (ready for the file phase, per-file or set-aware), or a
    terminal `ProcessResult` when the file is a duplicate or can't be dispatched.

    `extra_known_hashes` lets a batch catch INTRA-run duplicates: in the two-phase
    flow every file is catalogued before any is persisted, so the caller passes the
    sha256s already seen this run (the catalog alone wouldn't know about them yet).
    """
    operation_id = str(uuid4())
    current_state = INITIAL_STATE
    # The Inbox-relative folder the file was dropped in (e.g. "Water-Damage" for
    # Inbox/Water-Damage/photo.jpg; "" at the Inbox root). Recorded on the fiche
    # as the grouping signal for the organize phase — files dropped together in a
    # folder are a SET. The folder is a strong hint, not ground truth.
    try:
        relative_dir = source.parent.relative_to(paths.inbox_dir)
        source_folder = "" if relative_dir == Path(".") else str(relative_dir)
    except ValueError:
        source_folder = ""
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
    current_state = validate_transition(current_state, "INBOX_QUEUED")
    current_state = validate_transition(current_state, "PROCESSING_LOCKED")
    sha256 = _file_sha256(queued_target)
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    current_state = validate_transition(current_state, "ANALYSIS_RUNNING")

    if repo.has_sha256(sha256) or sha256 in extra_known_hashes:
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

    return _read_and_analyze(
        paths,
        queued_target=queued_target,
        source=source,
        source_folder=source_folder,
        sha256=sha256,
        dispatch=dispatch,
        operation_id=operation_id,
        current_state=current_state,
        now_utc=now_utc,
        features=features,
        emit=emit,
    )


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

    source = candidates[0]
    if dry_run:
        return _dry_run_one(paths, source, now_utc=now_utc, features=features, emit=emit)

    result = _catalog_one_inbox_file(paths, source, now_utc=now_utc, features=features, emit=emit)
    if isinstance(result, ProcessResult):
        return result  # duplicate or unreadable — already filed/trashed
    catdoc = result
    route_dir, pending_options, pending_reason = _route_from_analysis(
        paths, catdoc, now_utc=now_utc, features=features, emit=emit
    )
    return _file_cataloged(
        paths,
        catdoc,
        route_dir=route_dir,
        pending_options=pending_options,
        pending_reason=pending_reason,
        now_utc=now_utc,
        features=features,
        emit=emit,
    )


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
    flow_state = _process_next_inbox_file(paths, now_utc=now_utc, dry_run=dry_run, progress=progress).flow_state
    if not dry_run:
        _prune_empty_inbox_dirs(paths.inbox_dir)
    return flow_state


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

    existing_fiche = record.get("content_json")
    repo.upsert_document(
        doc_id=str(record["doc_id"]),
        sha256=str(record["sha256"]),
        current_filename=trash_target.name,
        current_path=str(trash_target),
        status=new_state,
        updated_at_utc=_utc_iso(now_utc),
        flow_state=new_state,
        content_json=str(existing_fiche) if existing_fiche else None,
    )
    _write_catalog_snapshot(paths, repo, now_utc, features=features)

    return new_state


class PendingDecisionError(RuntimeError):
    """Raised when a `review` resolution is rejected (invalid path, file gone)."""


def resolve_pending_decision(
    paths: RuntimePaths,
    record: dict[str, Any],
    chosen_label: str,
    *,
    now_utc: datetime | None = None,
) -> tuple[str, ...]:
    """Re-file one parked (DECISION_PENDING) document into the chosen category.

    `record` is a row from `list_pending_decisions()`. `chosen_label` is the
    user's pick — an existing option, an existing folder, or a brand-new path
    (a new subfolder, or a new root category, which is allowed ONLY here). The
    file moves out of Manual_Review into the chosen category, the catalog entry
    becomes LIBRARY_STORED with its pending decision cleared, and only now is the
    file mirrored (it is finally a settled placement).
    """
    ensure_runtime_layout(paths)
    features = load_feature_settings(paths)["features"]
    max_depth = load_runtime_policy(paths).taxonomy_max_depth

    target_route = normalize_review_path(chosen_label, max_depth)
    if target_route is None:
        raise PendingDecisionError(f"invalid category path: {chosen_label!r}")

    source = Path(str(record["current_path"]))
    if not source.exists() or not source.is_file():
        raise PendingDecisionError(f"file to re-file is missing: {source}")

    operation_id = str(uuid4())
    target_dir = paths.library_root / Path(*target_route)
    created_folder = not target_dir.exists()
    target_dir.mkdir(parents=True, exist_ok=True)
    library_target = _ensure_unique_path(target_dir / source.name)
    move(str(source), str(library_target))

    # Keep the document fiche, but update its category_path to reflect the
    # resolved destination (the AI proposed; the user decided).
    raw_fiche = record.get("content_json")
    content_json: str | None = None
    if raw_fiche:
        try:
            fiche = json.loads(raw_fiche)
        except (TypeError, ValueError):
            fiche = None
        if isinstance(fiche, dict):
            fiche["category_path"] = "/".join(target_route)
            content_json = json.dumps(fiche, ensure_ascii=True)
        else:
            content_json = str(raw_fiche)

    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    repo.upsert_document(
        doc_id=str(record["doc_id"]),
        sha256=str(record["sha256"]),
        current_filename=library_target.name,
        current_path=str(library_target),
        status="LIBRARY_STORED",
        updated_at_utc=_utc_iso(now_utc),
        flow_state="LIBRARY_STORED",
        pending_decision=None,  # decision resolved → leave the queue
        content_json=content_json,
    )
    _write_catalog_snapshot(paths, repo, now_utc, features=features)

    _append_action_log(
        paths,
        operation_id=operation_id,
        action="decision_resolved",
        status="success",
        message="User resolved a pending decision; file re-filed from review queue",
        now_utc=now_utc,
        path_before=str(source),
        path_after=str(library_target),
        extra_fields={
            "chosen_path": "/".join(target_route),
            "chosen_label": chosen_label,
            "created_folder": created_folder,
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
    return target_route


InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


def run_review(
    paths: RuntimePaths,
    *,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
    now_utc: datetime | None = None,
) -> dict[str, int]:
    """Interactively resolve the decisions queue (files the AI was unsure about).

    For each parked file we show the AI's options and let the user pick a number,
    type a custom path (new subfolder or new root category), or skip. `input_fn`
    / `output_fn` are injectable so the loop is testable without a real terminal.
    The caller is responsible for holding the runtime lock.
    """
    ensure_runtime_layout(paths)
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    pending = repo.list_pending_decisions()

    summary = {"pending": len(pending), "resolved": 0, "skipped": 0}
    if not pending:
        output_fn("No decisions pending.")
        return summary

    output_fn(f"{len(pending)} decision(s) pending.")
    for record in pending:
        raw_blob = record.get("pending_decision")
        try:
            data = json.loads(raw_blob) if raw_blob else {}
        except (TypeError, ValueError):
            data = {}
        options = [o for o in data.get("options", []) if isinstance(o, str)]
        reason = data.get("reason")
        snippet = (data.get("snippet") or "").strip().replace("\n", " ")

        output_fn("")
        output_fn(f"File: {record['current_filename']}")
        if reason:
            output_fn(f"  Why pending: {reason}")
        if snippet:
            output_fn(f"  Snippet: {snippet[:160]}")
        if options:
            output_fn("  Options:")
            for index, option in enumerate(options, start=1):
                output_fn(f"    {index}) {option}")
        else:
            output_fn("  (no AI options — type a category path)")
        output_fn("  Choose a number, or type a custom path (new subfolder or new root), or 's' to skip.")

        choice = (input_fn("  > ") or "").strip()
        if choice.lower() in {"", "s", "skip"}:
            summary["skipped"] += 1
            output_fn("  skipped")
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(options):
            chosen_label = options[int(choice) - 1]
        else:
            chosen_label = choice  # custom path (existing folder, new subfolder, or new root)

        try:
            route = resolve_pending_decision(paths, record, chosen_label, now_utc=now_utc)
        except PendingDecisionError as exc:
            output_fn(f"  error: {exc}")
            summary["skipped"] += 1
            continue
        summary["resolved"] += 1
        output_fn(f"  filed → {'/'.join(route)}")

    return summary


def _list_branch_files(
    candidate_dir: Path,
    *,
    max_files: int = 30,
    max_depth: int = 2,
) -> list[str]:
    """File names under `candidate_dir`, recursively to `max_depth`, newest-first.

    Symlinks are excluded — they may be the relocation markers created by M3 and
    must never be treated as documents. Depth 0 = files directly in candidate_dir;
    depth 2 = two levels below it (three directory levels total).
    """
    if not candidate_dir.is_dir():
        return []
    root = candidate_dir
    entries: list[tuple[float, str]] = []
    for dirpath, dirnames, filenames in os.walk(candidate_dir, followlinks=False):
        try:
            depth = len(Path(dirpath).relative_to(root).parts)
        except ValueError:
            dirnames.clear()
            continue
        if depth >= max_depth:
            dirnames.clear()  # prune — don't descend further
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append((mtime, name))
    entries.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in entries[:max_files]]


def _collect_candidate_branches(
    catdoc: _CatalogedDoc,
    paths: RuntimePaths,
    *,
    max_branches: int = 3,
) -> tuple[dict[str, list[str]], dict[str, Path]]:
    """Build the inputs for `propose_grouping`.

    Returns (candidate_branches, resolved_dirs):
    - candidate_branches: branch-path-str → list of existing filenames (for the prompt).
    - resolved_dirs: branch-path-str → actual directory Path (for file lookup in M3).

    Only branches that already exist on disk are included (empty ones are kept so
    the prompt shows them, but the caller's skip-if-all-empty guard still fires).
    At most `max_branches` (3) entries: category_path first, then alternatives.
    """
    if catdoc.analysis is None:
        return {}, {}
    candidates: list[str] = []
    if catdoc.analysis.category_path:
        candidates.append(catdoc.analysis.category_path)
    for alt in catdoc.analysis.alternatives:
        if alt not in candidates:
            candidates.append(alt)
        if len(candidates) >= max_branches:
            break
    candidate_branches: dict[str, list[str]] = {}
    resolved_dirs: dict[str, Path] = {}
    for path_str in candidates:
        validated = normalize_category_path(path_str, catdoc.max_depth)
        if validated is None:
            continue
        candidate_dir = paths.library_root / Path(*validated)
        if candidate_dir.is_dir():
            candidate_branches[path_str] = _list_branch_files(candidate_dir)
            resolved_dirs[path_str] = candidate_dir
    return candidate_branches, resolved_dirs


def _regroup_existing_file(
    paths: RuntimePaths,
    existing_filename: str,
    resolved_dirs: dict[str, Path],
    dest_dir: Path,
    *,
    operation_id: str,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> bool:
    """Move an existing LIBRARY_STORED file to `dest_dir`, leave a relative
    symlink at its old location, update the catalog and mirror copy.

    `resolved_dirs` comes from `_collect_candidate_branches`; we walk them to
    find `existing_filename` on disk. Returns True when the file was moved.
    Symlink failure (FS without support) is logged as a warning; the run continues.
    """
    existing_path: Path | None = None
    for branch_dir in resolved_dirs.values():
        for dirpath, _dirs, files in os.walk(branch_dir, followlinks=False):
            if existing_filename in files:
                candidate = Path(dirpath) / existing_filename
                if not candidate.is_symlink():
                    existing_path = candidate
                break
        if existing_path is not None:
            break

    if existing_path is None or not existing_path.is_file():
        emit(f"   ⚠ regroup: {existing_filename!r} not found on disk — skipping")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="regroup_file_not_found",
            status="warning",
            message=f"Cannot regroup: file not found on disk: {existing_filename}",
            now_utc=now_utc,
            features=features,
        )
        return False

    try:
        existing_path.resolve().relative_to(paths.library_root.resolve())
    except ValueError:
        emit(f"   ⚠ regroup: {existing_filename!r} outside library_root — refusing")
        return False

    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    record = repo.find_by_current_path(str(existing_path))
    if record is None:
        emit(f"   ⚠ regroup: {existing_filename!r} not in catalog — skipping")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="regroup_not_in_catalog",
            status="warning",
            message=f"Cannot regroup: no catalog entry for {existing_filename}",
            now_utc=now_utc,
            path_before=str(existing_path),
            features=features,
        )
        return False

    if record.get("status") != "LIBRARY_STORED":
        emit(f"   ⚠ regroup: {existing_filename!r} status={record.get('status')} ≠ LIBRARY_STORED — skipping")
        return False

    if existing_path.parent == dest_dir:
        # Already where the grouping wants it — moving it onto itself would only
        # rename it (__1) and leave a pointless symlink. Nothing to do.
        return False

    dest_dir.mkdir(parents=True, exist_ok=True)
    new_path = _ensure_unique_path(dest_dir / existing_path.name)
    old_path = existing_path
    move(str(old_path), str(new_path))

    try:
        rel_target = os.path.relpath(str(new_path), str(old_path.parent))
        old_path.symlink_to(rel_target)
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="symlink_left",
            status="success",
            message="Symlink left at old location after regroup",
            now_utc=now_utc,
            path_before=str(old_path),
            path_after=str(new_path),
            features=features,
        )
    except OSError as exc:
        emit(f"   ⚠ regroup: symlink at {old_path} failed: {exc}")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="symlink_failed",
            status="warning",
            message=f"Could not create symlink at old location: {exc}",
            now_utc=now_utc,
            path_before=str(old_path),
            path_after=str(new_path),
            features=features,
        )

    new_category_path = "/".join(new_path.relative_to(paths.library_root).parent.parts)
    existing_fiche = record.get("content_json")
    content_json: str | None = None
    if existing_fiche:
        try:
            fiche = json.loads(existing_fiche)
        except (TypeError, ValueError):
            fiche = None
        if isinstance(fiche, dict):
            fiche["category_path"] = new_category_path
            content_json = json.dumps(fiche, ensure_ascii=True)
        else:
            content_json = str(existing_fiche)

    now_iso = _utc_iso(now_utc)
    repo.upsert_document(
        doc_id=str(record["doc_id"]),
        sha256=str(record["sha256"]),
        current_filename=new_path.name,
        current_path=str(new_path),
        status="LIBRARY_STORED",
        updated_at_utc=now_iso,
        flow_state="LIBRARY_STORED",
        content_json=content_json,
    )
    _write_catalog_snapshot(paths, repo, now_utc, features=features)

    _append_action_log(
        paths,
        operation_id=operation_id,
        action="library_file_regrouped",
        status="success",
        message="Existing library file moved to common series folder",
        now_utc=now_utc,
        path_before=str(old_path),
        path_after=str(new_path),
        features=features,
    )
    emit(f"   regrouped → {new_path.relative_to(paths.library_root)}")

    # Move the mirror copy (if any). Symlinks are never mirrored.
    try:
        old_relative = old_path.relative_to(paths.library_root)
        old_mirror = paths.mirror_root / old_relative
        if old_mirror.exists() and old_mirror.is_file() and not old_mirror.is_symlink():
            new_relative = new_path.relative_to(paths.library_root)
            new_mirror = _ensure_unique_path(paths.mirror_root / new_relative)
            new_mirror.parent.mkdir(parents=True, exist_ok=True)
            move(str(old_mirror), str(new_mirror))
            _append_action_log(
                paths,
                operation_id=operation_id,
                action="mirror_regrouped",
                status="success",
                message="Mirror copy moved with regrouped file",
                now_utc=now_utc,
                path_before=str(old_mirror),
                path_after=str(new_mirror),
                features=features,
            )
    except Exception as exc:  # noqa: BLE001
        emit(f"   ⚠ mirror regroup failed: {exc}")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="mirror_regroup_failed",
            status="warning",
            message=f"Mirror regroup failed: {exc}",
            now_utc=now_utc,
            features=features,
        )

    return True


# R7 scale guard: max documents sent to the organizer in one call. A folder set
# larger than this is processed in batches so a single huge call can't error out.
# Normal folders stay well under it and are organized in ONE call (whole set).
ORGANIZE_MAX_SET = 80


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
        "pending_decisions": 0,
        "organized": 0,
        "regrouped": 0,
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

    emit: ProgressFn = progress or (lambda _message: None)

    def _tally(result: ProcessResult, *, organized: bool = False) -> None:
        summary["total"] += 1
        if result.pending:
            summary["pending_decisions"] += 1
        elif result.flow_state == "LIBRARY_STORED":
            summary["processed"] += 1
            if organized:
                summary["organized"] += 1
        elif result.flow_state == "INBOX_TRASH_PENDING_MANUAL":
            summary["duplicates"] += 1
        elif result.flow_state == "USER_CONFIRMATION_REQUIRED":
            summary["manual_reviews"] += 1
        elif result.flow_state.startswith("ERROR"):
            summary["errors"] += 1
        if result.mirror_failed:
            summary["mirror_failures"] += 1

    def _record_error(exc: Exception) -> None:
        # The risky steps (reading, AI calls) all run after the file has left the
        # Inbox, so the offending file is already consumed — log it, count it, move on.
        summary["total"] += 1
        summary["errors"] += 1
        _append_action_log(
            paths,
            operation_id=str(uuid4()),
            action="process_error",
            status="error",
            message=f"Unexpected error processing a file, skipping: {exc}",
            now_utc=now_utc,
            features=features,
        )
        emit(f"   ✗ error, skipping: {exc}")

    # Two-phase, set-aware processing (Option B). Files dropped together in a
    # top-level Inbox subfolder are a SET: CATALOG every file of the set first
    # (read+analyze → fiche, no filing), THEN ORGANIZE the whole set at once so a
    # coherent group is placed together — nothing scatters or leaks to the
    # decisions queue file-by-file. Files loose in the Inbox root are singletons,
    # classified one by one. One bad file never aborts the batch.
    folder_sets: dict[str, list[Path]] = {}
    singletons: list[Path] = []
    for candidate in _iter_inbox_files(paths.inbox_dir):
        try:
            relative_dir = candidate.parent.relative_to(paths.inbox_dir)
        except ValueError:
            relative_dir = Path(".")
        if relative_dir == Path("."):
            singletons.append(candidate)
        else:
            folder_sets.setdefault(relative_dir.parts[0], []).append(candidate)

    # Each top-level subfolder = one set; each loose root file = its own singleton.
    work_sets: list[tuple[str, list[Path]]] = [(top, members) for top, members in folder_sets.items()]
    work_sets += [("", [loose]) for loose in singletons]

    organize_chain = task_chain_from_env("ORGANIZE")
    max_depth = load_runtime_policy(paths).taxonomy_max_depth
    base_categories = [category_label(c) for c in classifiable_categories()]
    user_context = load_user_context()
    run_seen: set[str] = set()

    for set_top, sources in work_sets:
        # Phase 1 — CATALOG every file of the set (no filing yet).
        catdocs: list[_CatalogedDoc] = []
        for source in sources:
            try:
                outcome = _catalog_one_inbox_file(
                    paths, source, now_utc=now_utc, features=features, emit=emit, extra_known_hashes=run_seen
                )
            except Exception as exc:  # noqa: BLE001 — one bad file must never abort the batch
                _record_error(exc)
                continue
            if isinstance(outcome, ProcessResult):
                _tally(outcome)  # duplicate or unreadable — already trashed/filed
                continue
            run_seen.add(outcome.sha256)
            catdocs.append(outcome)

        # Phase 2 — ORGANIZE the whole set at once (real folder-sets only, and only
        # when an ORGANIZE chain is configured). The set's coherence decides each
        # placement; the drop-folder is a strong-but-overridable hypothesis. A
        # singleton root file or a missing chain → per-file route (no grouping).
        organized: dict[int, str | None] = {}
        analyzable = [(i, c) for i, c in enumerate(catdocs) if c.analysis is not None]
        if organize_chain and set_top and analyzable:
            # R7 (scale): one organize call over a huge folder is error-prone; cap
            # the batch size and chunk. Normal folders (well under the cap) stay in
            # a SINGLE call, so the whole set is decided together.
            existing_paths = existing_category_paths(paths.library_root)
            for batch_start in range(0, len(analyzable), ORGANIZE_MAX_SET):
                batch = analyzable[batch_start : batch_start + ORGANIZE_MAX_SET]
                documents = [
                    {
                        "name": c.analysis.name,
                        "summary": c.analysis.summary,
                        "document_date": c.analysis.document_date,
                        "category_path": c.analysis.category_path,
                        "original_filename": c.source.name,
                    }
                    for _, c in batch
                ]
                try:
                    org_result = organize_set(
                        documents,
                        base_categories=base_categories,
                        existing_paths=existing_paths,
                        source_folder=set_top,
                        user_context=user_context,
                    )
                    for local_pos, (cat_idx, _) in enumerate(batch):
                        organized[cat_idx] = org_result.placements.get(local_pos)
                except Exception as exc:  # noqa: BLE001 — organize failure → per-file fallback
                    emit(f"   ✗ organize failed, per-file fallback: {exc}")

        # Phase 3 — FILE each catalogued doc into its final placement.
        for index, catdoc in enumerate(catdocs):
            try:
                organized_path = organized.get(index)
                used_organize = (
                    organized_path is not None and normalize_category_path(organized_path, max_depth) is not None
                )
                route_dir, pending_options, pending_reason = _route_from_analysis(
                    paths, catdoc, organized_path=organized_path, now_utc=now_utc, features=features, emit=emit
                )

                # M2+M3 — singleton-only grouping: compare this new file's name
                # against existing files along its candidate branches; propose a
                # shared series folder and regroup existing files into it (M3).
                # Skipped for folder-sets (organizer already handles them), for
                # pending decisions, for manual review, and for no-analysis files.
                if (
                    not set_top
                    and catdoc.analysis is not None
                    and pending_options is None
                    and tuple(route_dir) != tuple(INTERIM_LIBRARY_DIR)
                ):
                    candidate_branches, resolved_dirs = _collect_candidate_branches(catdoc, paths)
                    if candidate_branches:
                        grouping = propose_grouping(
                            {
                                "name": catdoc.analysis.name,
                                "summary": catdoc.analysis.summary,
                                "original_filename": catdoc.source.name,
                            },
                            candidate_branches,
                        )
                        if not grouping.used_fallback and grouping.path is not None:
                            validated_gp = normalize_category_path(grouping.path, max_depth)
                            if validated_gp is not None:
                                route_dir = validated_gp
                                dest_dir = paths.library_root / Path(*validated_gp)
                                for existing_name in grouping.group_with:
                                    ok = _regroup_existing_file(
                                        paths,
                                        existing_name,
                                        resolved_dirs,
                                        dest_dir,
                                        operation_id=catdoc.operation_id,
                                        now_utc=now_utc,
                                        features=features,
                                        emit=emit,
                                    )
                                    if ok:
                                        summary["regrouped"] += 1

                result = _file_cataloged(
                    paths,
                    catdoc,
                    route_dir=route_dir,
                    pending_options=pending_options,
                    pending_reason=pending_reason,
                    now_utc=now_utc,
                    features=features,
                    emit=emit,
                )
            except Exception as exc:  # noqa: BLE001
                _record_error(exc)
                continue
            _tally(result, organized=used_organize and result.flow_state == "LIBRARY_STORED")

    # Tidy up: drop the now-empty Inbox subfolders the processed files left behind.
    _prune_empty_inbox_dirs(paths.inbox_dir)

    _append_action_log(
        paths,
        operation_id=str(uuid4()),
        action="process_all_summary",
        status="success",
        message=(
            "Batch completed: "
            f"processed={summary['processed']}, duplicates={summary['duplicates']}, "
            f"manual_reviews={summary['manual_reviews']}, pending_decisions={summary['pending_decisions']}, "
            f"organized={summary['organized']}, regrouped={summary['regrouped']}, "
            f"errors={summary['errors']}, mirror_failures={summary['mirror_failures']}"
        ),
        now_utc=now_utc,
        extra_fields={"dry_run": False},
        features=features,
    )

    return summary
