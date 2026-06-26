# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2, move
from typing import Any
from uuid import uuid4

# Optional live-progress callback: the CLI passes one to stream human-readable
# lines as each file is processed (so the user can watch and interrupt). The
# pipeline never prints directly — it only calls this when given.
ProgressFn = Callable[[str], None]

from procrafiler.catalog import CatalogRepository
from procrafiler.config import (
    RuntimePaths,
    ensure_runtime_layout,
    get_deletion_mode,
    get_user_language,
    load_feature_settings,
    load_runtime_policy,
)
from procrafiler.ai_analysis import analyze_content, translate_keywords  # type: ignore[reportMissingImports]
from procrafiler.ai_grouping import propose_grouping  # type: ignore[reportMissingImports]
from procrafiler.ai_organize import organize_set  # type: ignore[reportMissingImports]
from procrafiler.user_context import load_user_context  # type: ignore[reportMissingImports]
from procrafiler.ai_naming import task_chain_from_env  # type: ignore[reportMissingImports]
from procrafiler.ai_reader import read_with_ocr, read_with_vision  # type: ignore[reportMissingImports]
from procrafiler.content_reader import extract_text_content
from procrafiler.flow import INITIAL_STATE, validate_transition
from procrafiler.mirror import sync_library_file_to_mirror  # type: ignore[reportMissingImports]
from procrafiler.search_index import BodyTextIndex
from procrafiler.naming import build_timestamped_filename, has_timestamp_prefix, sanitize_filename_stem
from procrafiler.taxonomy import (  # type: ignore[reportMissingImports]
    INTERIM_LIBRARY_DIR,
    base_category_for,
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


_YEAR_RE = re.compile(r"^\d{4}$")


def _with_series_year(
    route_dir: tuple[str, ...], *, series: bool, year: str | None
) -> tuple[str, ...]:
    """Append a bare-YEAR subfolder to a SERIES document's ENTITY folder.

    The year is owned by the CODE (derived from the document's own date), not by
    the AI: the prompts propose only the entity folder. No-op when the document
    is not a series, when `route_dir` is at or above a base (no entity folder to
    date — e.g. a cert filed flat in `Education`), when the last segment is
    already a year, or when no usable year is available.
    """
    if not series or not year or not _YEAR_RE.match(year):
        return route_dir
    base = base_category_for(route_dir)
    if base is None or len(route_dir) <= len(base):
        return route_dir
    if _YEAR_RE.match(route_dir[-1]):
        return route_dir
    return route_dir + (year,)


def _with_series_entity(
    route_dir: tuple[str, ...], *, series: bool, issuer: str | None, library_root: Path
) -> tuple[str, ...]:
    """Deterministic safety net for a SERIES the AI routed to a BARE BASE.

    The AI normally proposes the entity folder itself (and reuses an existing one
    from the tree — that is what keeps a series together over time). But when it
    under-proposes — stopping at the base, leaving e.g. an EDF and an Enercoop
    bill loose in `Utilities` for the grouping to wrongly merge — and we know the
    issuer, append it as the entity folder so two issuers NEVER share a base
    folder. Reuses an existing sibling that matches the issuer (case-insensitive)
    to avoid a near-duplicate. No-op when not a series, when the route already has
    an entity folder below its base, or when no issuer is known.
    """
    if not series or not issuer:
        return route_dir
    base = base_category_for(route_dir)
    if base is None or len(route_dir) > len(base):
        return route_dir  # no base, or already has an entity folder below the base
    slug = sanitize_filename_stem(issuer)
    if not slug:
        return route_dir
    base_dir = library_root / Path(*base)
    if base_dir.is_dir():
        for child in sorted(base_dir.iterdir()):
            if child.is_dir() and child.name.lower() == slug.lower():
                return base + (child.name,)  # reuse the existing issuer folder
    return base + (slug,)


def _fiche_year(content_json: Any) -> str | None:
    """Extract a 4-digit year from a stored fiche — the document's own date
    (document_date, else the resolved effective_date). Used to date an EXISTING
    file when a regroup moves it into a series folder."""
    if not content_json:
        return None
    try:
        fiche = json.loads(content_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(fiche, dict):
        return None
    for key in ("document_date", "effective_date"):
        value = fiche.get(key)
        if isinstance(value, str) and _YEAR_RE.match(value[:4]):
            return value[:4]
    return None


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
    target: Path | None = None,
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
            "library_root": str(paths.library_root),
            "documents_count": len(documents),
            "last_update_utc": latest,
        },
        "documents": documents,
    }
    target_path = target or paths.catalog_snapshot_file
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(target_path)


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
        # Back up the hidden text sidecar too (if any), so the costly OCR/vision
        # text is mirrored alongside its document.
        _mirror_text_sidecar(paths, library_file)
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
    # Where the file landed in the library, when it did. The batch records these
    # as THIS RUN's placements: a location created during the run is not a
    # pre-run reference, so regrouping from it leaves no symlink (spec §1.2).
    library_path: str | None = None


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
            user_language=get_user_language(paths),
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


# --- Text sidecars (Search Slice 2) -----------------------------------------
# For a document whose text could only be obtained by AI (OCR for a scanned PDF,
# vision for an image), we keep that extracted text ONCE in a HIDDEN sidecar next
# to the file (`.<filename>.txt`), so a future deep search can read the content
# without ever re-OCR/re-vision (respecting "process once"). Plain text files and
# readable PDFs need no sidecar — their text is free to re-extract. The sidecar is
# hidden, so rescan's walk ignores it; rescan moves/removes it with its document.

def _sidecar_path(doc_path: Path) -> Path:
    return doc_path.parent / ("." + doc_path.name + ".txt")


def _write_text_sidecar(doc_path: Path, read_via: str | None, content_text: str | None) -> None:
    """Write the AI-extracted text next to `doc_path` (hidden) — only when the
    text came from OCR or vision (costly, not re-derivable for free)."""
    if read_via not in ("ocr", "vision") or not content_text or not content_text.strip():
        return
    try:
        _sidecar_path(doc_path).write_text(content_text, encoding="utf-8")
    except OSError:
        pass


def _move_text_sidecar(old_doc_path: Path, new_doc_path: Path) -> None:
    """Follow a document's hand move/rename with its hidden text sidecar. The
    sidecar name stays exactly `.<filename>.txt` (the document names are already
    unique, so there is no collision to disambiguate)."""
    old = _sidecar_path(old_doc_path)
    new = _sidecar_path(new_doc_path)
    if old == new or not old.exists():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        move(str(old), str(new))
    except OSError:
        pass


def _mirror_text_sidecar(paths: RuntimePaths, library_file: Path) -> None:
    """Back up a document's hidden text sidecar to the mirror, alongside the
    mirror copy of the document — so the costly OCR/vision text survives even if
    the primary library is lost. No-op when the document has no sidecar."""
    sidecar = _sidecar_path(library_file)
    if not sidecar.is_file():
        return
    try:
        relative_path = sidecar.relative_to(paths.library_root)
    except ValueError:
        return
    mirror_target = paths.mirror_root / relative_path
    try:
        mirror_target.parent.mkdir(parents=True, exist_ok=True)
        copy2(str(sidecar), str(mirror_target))
    except OSError:
        pass


def _trash_deleted_artifacts(
    paths: RuntimePaths,
    doc_path: Path,
    *,
    operation_id: str,
    now_utc: datetime | None,
    features: dict[str, bool],
) -> None:
    """When a library document is deleted by hand, quarantine its leftover
    artifacts — the document itself is already gone (the user deleted it). Each
    artifact goes to ITS OWN library's trash (same rule as
    `move_library_file_to_trash`): the mirror backup copy and the mirror's text
    sidecar to `Mirror_Trash`, and the primary hidden text sidecar to the primary
    `Library_Trash`."""
    try:
        relative_path = doc_path.relative_to(paths.library_root)
    except ValueError:
        return
    sidecar = _sidecar_path(doc_path)
    sidecar_rel = sidecar.relative_to(paths.library_root)
    artifacts = (
        (paths.mirror_root / relative_path, paths.mirror_trash_dir, relative_path,
         "library_deleted_mirror_quarantined", "Mirror copy quarantined to Mirror_Trash on hand deletion."),
        (paths.mirror_root / sidecar_rel, paths.mirror_trash_dir, sidecar_rel,
         "library_deleted_mirror_sidecar_quarantined", "Mirror text sidecar quarantined to Mirror_Trash on hand deletion."),
        (sidecar, paths.library_trash_manual_dir, sidecar_rel,
         "library_deleted_sidecar_quarantined", "Hidden text sidecar quarantined to Library_Trash on hand deletion."),
    )
    for source, trash_dir, rel, action, message in artifacts:
        if not source.is_file():
            continue
        target = _ensure_unique_path(trash_dir / rel)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            move(str(source), str(target))
        except OSError:
            continue
        _append_action_log(
            paths, operation_id=operation_id, action=action, status="success",
            message=message,
            now_utc=now_utc, path_before=str(source), path_after=str(target), features=features,
        )


def _move_mirror_copy(
    paths: RuntimePaths,
    old_doc_path: Path,
    new_doc_path: Path,
    *,
    operation_id: str,
    now_utc: datetime | None,
    features: dict[str, bool],
) -> None:
    """Follow a document's hand move/rename with its mirror copy (and the mirror's
    text sidecar), so the mirror stays a faithful path-for-path replica of the
    library. Without this, a hand move would orphan the mirror copy at the OLD
    path and leave NOTHING at the new one (a later scrub would then see the mirror
    'missing' at the new path and the stale old copy lingering). The destination is
    the exact mirror path — no disambiguation — so scrub finds it where it expects.
    No-op when the mirror copy is absent (mirror off / out of sync is tolerated)."""
    try:
        old_rel = old_doc_path.relative_to(paths.library_root)
        new_rel = new_doc_path.relative_to(paths.library_root)
    except ValueError:
        return
    old_sidecar_rel = _sidecar_path(old_doc_path).relative_to(paths.library_root)
    new_sidecar_rel = _sidecar_path(new_doc_path).relative_to(paths.library_root)
    moves = (
        (paths.mirror_root / old_rel, paths.mirror_root / new_rel,
         "library_moved_mirror_followed", "Mirror copy moved to follow the hand move/rename (mirror kept in sync)."),
        (paths.mirror_root / old_sidecar_rel, paths.mirror_root / new_sidecar_rel,
         "library_moved_mirror_sidecar_followed", "Mirror text sidecar moved to follow the hand move/rename."),
    )
    for source, target, action, message in moves:
        if source == target or not source.is_file():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            move(str(source), str(target))
        except OSError:
            continue
        _append_action_log(
            paths, operation_id=operation_id, action=action, status="success",
            message=message,
            now_utc=now_utc, path_before=str(source), path_after=str(target), features=features,
        )


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
    override_name: str | None = None,
) -> ProcessResult:
    """Name, date, move the file into `route_dir`, persist its fiche, and mirror.
    The route was decided by the caller (per-file or set-aware organize).
    `override_name`, when given, replaces the analysis name as the file's stem —
    used when grouping aligns a new file to the series it joins (3a)."""
    analysis = catdoc.analysis
    queued_target = catdoc.queued_target
    source = catdoc.source
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()

    if override_name:
        stem = sanitize_filename_stem(override_name)
    elif analysis is not None and analysis.name:
        stem = analysis.name
    else:
        stem = sanitize_filename_stem(Path(queued_target.name).stem)
    document_date = analysis.document_date if analysis is not None else None
    document_dt = _resolve_document_date(document_date, queued_target, now_utc, media_type=catdoc.dispatch.media_type)

    # SERIES placement, owned by the code so it can't be dropped/guessed:
    # 1) if the AI under-routed a series to a bare base, push it into its issuer
    #    entity folder (so two issuers never share a base folder); then
    # 2) append the dated year subfolder from the document's own date
    #    (document_dt — EXIF/content date, never the processing timestamp).
    series = bool(analysis.series) if analysis is not None else False
    issuer = analysis.entities.get("issuer") if analysis is not None and isinstance(analysis.entities, dict) else None
    route_dir = _with_series_entity(
        route_dir, series=series, issuer=issuer if isinstance(issuer, str) else None, library_root=paths.library_root
    )
    route_dir = _with_series_year(route_dir, series=series, year=document_dt.strftime("%Y"))

    target_dir = paths.library_root / Path(*route_dir)
    if not target_dir.exists():
        emit(f"   created folder: {'/'.join(route_dir)}")
    target_dir.mkdir(parents=True, exist_ok=True)

    candidate_name = f"{stem}{queued_target.suffix}"
    final_name = build_timestamped_filename(candidate_name, now_utc=document_dt)
    library_target = _ensure_unique_path(target_dir / final_name)
    move(str(queued_target), str(library_target))
    current_state = validate_transition(catdoc.current_state, "LIBRARY_STORED")
    emit(f"   filed → {'/'.join(route_dir)}/{library_target.name}")

    now_iso = _utc_iso(now_utc)
    is_routed = tuple(route_dir) != tuple(INTERIM_LIBRARY_DIR)
    fiche: dict[str, Any] = {
        "name": stem if override_name else (analysis.name if analysis is not None else None),
        "document_date": document_date,
        "series": series,
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

    # The AI-extracted text exists only now (read time); keep its sidecar even for
    # a file parked in review — `resolve_pending_decision` moves it on resolution.
    _write_text_sidecar(library_target, catdoc.read_via, catdoc.content_text)
    if is_pending:
        return ProcessResult(current_state, mirror_failed=False, pending=True, library_path=str(library_target))

    mirror_status = _sync_to_mirror(
        paths,
        operation_id=catdoc.operation_id,
        library_file=library_target,
        now_utc=now_utc,
        features=features,
    )
    return ProcessResult(
        current_state,
        mirror_failed=mirror_status == MIRROR_FAILED,
        doc_id=doc_id,
        library_path=str(library_target),
    )


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
    if repo.has_live_sha256(sha256):
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
    terminal `ProcessResult` when the file is a duplicate. An undispatchable file
    (unsupported/missing extension) becomes a no-analysis `_CatalogedDoc` that the
    file phase places in Manual_Review — never left stranded in the Queue.

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

    if repo.has_live_sha256(sha256) or sha256 in extra_known_hashes:
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

    # Not a duplicate of a LIVE document. If its content matches a tombstone, it is
    # a RE-DEPOSIT of something you deleted — file it normally, but tell you.
    deleted_at = repo.deleted_at_for_sha256(sha256)
    if deleted_at:
        emit(f"   note: you previously deleted this file (on {deleted_at[:10]}) — re-filing it")
        _append_action_log(
            paths, operation_id=operation_id, action="redeposit_of_deleted", status="success",
            message=f"Re-deposit of content previously deleted on {deleted_at}",
            now_utc=now_utc, path_before=str(queued_target),
            extra_fields={"deleted_at": deleted_at}, features=features,
        )

    current_state = validate_transition(current_state, "CLASSIFICATION_READY")
    current_state = validate_transition(current_state, "ROUTE_PROPOSED")

    dispatch = dispatch_for_filename(queued_target.name)
    if not dispatch.can_dispatch:
        # Unsupported/missing extension: no reader can open it, so the AI never
        # sees it. Route it to Manual_Review (the catch-all for unreadable
        # content) like an AI-unreadable file — a no-analysis _CatalogedDoc that
        # the file phase places there. NEVER leave it stranded in the Queue
        # (invisible to `review`, skipped by the next run).
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
        return _CatalogedDoc(
            queued_target=queued_target,
            source=source,
            source_folder=source_folder,
            sha256=sha256,
            dispatch=dispatch,
            operation_id=operation_id,
            current_state=validate_transition(current_state, "ROUTE_CONFIRMED"),
            content_text=None,
            read_via=None,
            analysis=None,
            max_depth=load_runtime_policy(paths).taxonomy_max_depth,
        )

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

    # The hidden text sidecars follow their document to trash, each to its own
    # library's trash: primary sidecar → Library_Trash, mirror sidecar → Mirror_Trash.
    sidecar_rel = _sidecar_path(relative_path)
    for sidecar_source, trash_dir, sidecar_action, sidecar_message in (
        (_sidecar_path(resolved), paths.library_trash_manual_dir,
         "library_trash_sidecar_quarantined", "Hidden text sidecar moved to Library_Trash with its document."),
        (_sidecar_path(mirror_source), paths.mirror_trash_dir,
         "library_trash_mirror_sidecar_quarantined", "Mirror text sidecar quarantined to Mirror_Trash with its document."),
    ):
        if not sidecar_source.is_file():
            continue
        sidecar_target = _ensure_unique_path(trash_dir / sidecar_rel)
        sidecar_target.parent.mkdir(parents=True, exist_ok=True)
        move(str(sidecar_source), str(sidecar_target))
        _append_action_log(
            paths, operation_id=operation_id, action=sidecar_action, status="success",
            message=sidecar_message, now_utc=now_utc,
            path_before=str(sidecar_source), path_after=str(sidecar_target), features=features,
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
    _move_text_sidecar(source, library_target)  # the hidden text copy follows out of review

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
    """Paths of files under `candidate_dir` (RELATIVE to it), to `max_depth`,
    newest-first.

    Relative paths — not bare names — so the grouping model sees WHERE inside
    the branch each file lives (an existing series subfolder is visible as
    `Releves-eau/2026-01__Releve.pdf`), and so a `group_with` answer cites an
    unambiguous path. Symlinks are excluded — they may be the relocation
    markers left by a regroup and must never be treated as documents. Depth 0 =
    files directly in candidate_dir; depth 2 = two levels below it.
    """
    if not candidate_dir.is_dir():
        return []
    root = candidate_dir
    entries: list[tuple[float, str]] = []
    for dirpath, dirnames, filenames in os.walk(candidate_dir, followlinks=False):
        try:
            rel_dir = Path(dirpath).relative_to(root)
        except ValueError:
            dirnames.clear()
            continue
        if len(rel_dir.parts) >= max_depth:
            dirnames.clear()  # prune — don't descend further
        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append((mtime, (rel_dir / name).as_posix()))
    entries.sort(key=lambda x: x[0], reverse=True)
    return [rel for _, rel in entries[:max_files]]


def _collect_candidate_branches(
    catdoc: _CatalogedDoc,
    paths: RuntimePaths,
    *,
    max_branches: int = 3,
) -> tuple[dict[str, list[str]], dict[str, Path]]:
    """Build the inputs for `propose_grouping`.

    Returns (candidate_branches, resolved_dirs), both keyed by the branch's
    normalized label (e.g. "Personal/Administrative/Housing"):
    - candidate_branches: label → existing files inside (paths relative to the
      branch, for the prompt).
    - resolved_dirs: label → the branch directory Path (for file lookup).

    A candidate that does not exist on disk yet (e.g. the series subfolder M1
    just proposed) is replaced by its NEAREST EXISTING ANCESTOR — that is where
    the files to regroup live. At most `max_branches` (3) candidates are
    considered: category_path first, then alternatives.
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
        # Walk up to the nearest existing directory (never above the base's
        # first segment — bases always exist via ensure_runtime_layout).
        parts = list(validated)
        while parts and not (paths.library_root / Path(*parts)).is_dir():
            parts.pop()
        if not parts:
            continue
        label = "/".join(parts)
        if label in candidate_branches:
            continue
        branch_dir = paths.library_root / Path(*parts)
        candidate_branches[label] = _list_branch_files(branch_dir)
        resolved_dirs[label] = branch_dir
    return candidate_branches, resolved_dirs


_TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}__")


def _strip_ts_prefix(name: str) -> str:
    return _TS_PREFIX_RE.sub("", name)


def _find_listed_file(
    existing_ref: str,
    candidate_branches: dict[str, list[str]],
    resolved_dirs: dict[str, Path],
) -> Path | None:
    """Resolve a `group_with` reference to a real file among the LISTED branch
    files — never guess.

    First an exact branch-relative path (what the prompt asks the model to
    copy); then, tolerance for a model citing just the file name or dropping
    the timestamp prefix — accepted only when the match is UNIQUE across all
    listed files.
    """
    ref = existing_ref.strip().strip("/")
    if not ref:
        return None
    for label, branch_dir in resolved_dirs.items():
        if ref in candidate_branches.get(label, []):
            candidate = branch_dir / ref
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
    ref_name = _strip_ts_prefix(Path(ref).name)
    hits: list[Path] = []
    seen: set[str] = set()
    for label, rels in candidate_branches.items():
        for rel in rels:
            if _strip_ts_prefix(Path(rel).name) == ref_name:
                candidate = resolved_dirs[label] / rel
                if str(candidate) not in seen:
                    seen.add(str(candidate))
                    hits.append(candidate)
    if len(hits) == 1 and hits[0].is_file() and not hits[0].is_symlink():
        return hits[0]
    return None


def _regroup_existing_file(
    paths: RuntimePaths,
    existing_ref: str,
    candidate_branches: dict[str, list[str]],
    resolved_dirs: dict[str, Path],
    dest_dir: Path,
    *,
    operation_id: str,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
    run_placed: set[str],
    run_symlinks: dict[str, Path],
    series_year: bool = False,
) -> bool:
    """Move an existing LIBRARY_STORED file DEEPER into `dest_dir`, update the
    catalog and mirror copy, and leave a relative symlink at its old location.

    Two run-invariant guards (spec §1.2):
    - DEEPEN-ONLY: `dest_dir` must be a STRICT descendant of the file's current
      folder. A run may only increase order — never flatten, move up, or cross
      branches; anything else is refused and logged.
    - SYMLINK = PRE-RUN REFERENCE ONLY: a symlink preserves a location the user
      knew BEFORE the run. `run_placed` holds the paths this run created — a
      file regrouped from one of those moves WITHOUT leaving a symlink, and a
      symlink this run already created (`run_symlinks`: target → link) is
      RETARGETED if its file moves again, never left dangling.

    Returns True when the file was moved. Symlink failure (FS without support)
    is logged as a warning; the run continues.
    """
    existing_path = _find_listed_file(existing_ref, candidate_branches, resolved_dirs)
    if existing_path is None:
        emit(f"   ⚠ regroup: {existing_ref!r} not found among the listed files — skipping")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="regroup_file_not_found",
            status="warning",
            message=f"Cannot regroup: no unique listed file matches: {existing_ref}",
            now_utc=now_utc,
            features=features,
        )
        return False

    try:
        existing_path.resolve().relative_to(paths.library_root.resolve())
    except ValueError:
        emit(f"   ⚠ regroup: {existing_ref!r} outside library_root — refusing")
        return False

    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    record = repo.find_by_current_path(str(existing_path))
    if record is None:
        emit(f"   ⚠ regroup: {existing_ref!r} not in catalog — skipping")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="regroup_not_in_catalog",
            status="warning",
            message=f"Cannot regroup: no catalog entry for {existing_ref}",
            now_utc=now_utc,
            path_before=str(existing_path),
            features=features,
        )
        return False

    if record.get("status") != "LIBRARY_STORED":
        emit(f"   ⚠ regroup: {existing_ref!r} status={record.get('status')} ≠ LIBRARY_STORED — skipping")
        return False

    # When the destination is a SERIES folder, the moved file lands in its OWN
    # dated year subfolder (from its catalog date) — same deterministic rule as
    # a freshly filed series document. A file already in its year folder then
    # fails the deepen-only guard below and stays put (correct).
    if series_year:
        dest_route = _with_series_year(
            dest_dir.relative_to(paths.library_root).parts,
            series=True,
            year=_fiche_year(record.get("content_json")),
        )
        dest_dir = paths.library_root / Path(*dest_route)

    try:
        depth_gain = dest_dir.relative_to(existing_path.parent)
    except ValueError:
        depth_gain = None
    if depth_gain is None or not depth_gain.parts:
        # Not a strict descendant of the file's current folder: moving it would
        # flatten or cross branches — exactly what a run must never do.
        emit(f"   ⚠ regroup refused (not deeper): {existing_ref!r} stays where it is")
        _append_action_log(
            paths,
            operation_id=operation_id,
            action="regroup_refused_not_deeper",
            status="warning",
            message="Regroup refused: destination is not strictly deeper than the file's folder",
            now_utc=now_utc,
            path_before=str(existing_path),
            path_after=str(dest_dir),
            features=features,
        )
        return False

    dest_dir.mkdir(parents=True, exist_ok=True)
    new_path = _ensure_unique_path(dest_dir / existing_path.name)
    old_path = existing_path
    move(str(old_path), str(new_path))

    placed_this_run = str(old_path) in run_placed
    run_placed.add(str(new_path))
    earlier_symlink = run_symlinks.pop(str(old_path), None)
    if placed_this_run:
        # The old location only ever existed within this run — nobody knew it,
        # so no marker there. But a symlink this run left at the file's PRE-RUN
        # location must follow the file instead of dangling.
        if earlier_symlink is not None and earlier_symlink.is_symlink():
            try:
                earlier_symlink.unlink()
                earlier_symlink.symlink_to(os.path.relpath(str(new_path), str(earlier_symlink.parent)))
                run_symlinks[str(new_path)] = earlier_symlink
            except OSError as exc:
                emit(f"   ⚠ regroup: retargeting symlink {earlier_symlink} failed: {exc}")
    else:
        try:
            old_path.symlink_to(os.path.relpath(str(new_path), str(old_path.parent)))
            run_symlinks[str(new_path)] = old_path
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

# Above this many files in one go, warn the user before the heavy work: each file
# is read by AI (API cost, or local CPU/GPU time). It is only a HEADS-UP — nothing
# is gated; library files still get classified like an Inbox batch.
LARGE_BATCH_WARN = 25

# Index-only pass (VCS repos): which media types are worth reading into the
# catalog for search, and a size ceiling. A repo working tree is mostly small
# text docs (.md/.txt/.sh) and code; we index the readable documents and skip
# binaries/images/huge files. The files themselves are NEVER renamed or moved.
INDEXABLE_MEDIA_TYPES = ("text", "pdf")
INDEX_MAX_BYTES = 2_000_000


def _index_preserve_file_in_place(
    paths: RuntimePaths,
    file_path: Path,
    *,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> bool:
    """Index a PRESERVE-ZONE document (a VCS repo's working tree, or an Archive
    folder) INTO THE CATALOG for search, WITHOUT touching it — no rename, no move,
    no timestamp prefix (that would defeat the point). Only readable document types
    under the size ceiling are read; anything else is skipped. Returns True when a
    row was written."""
    dispatch = dispatch_for_filename(file_path.name)
    if dispatch.media_type not in INDEXABLE_MEDIA_TYPES:
        return False
    try:
        if file_path.stat().st_size > INDEX_MAX_BYTES:
            return False
    except OSError:
        return False

    op = str(uuid4())
    sha256 = _file_sha256(file_path)
    catdoc = _read_and_analyze(
        paths,
        queued_target=file_path,
        source=file_path,
        source_folder=file_path.parent.name,
        sha256=sha256,
        dispatch=dispatch,
        operation_id=op,
        current_state="ROUTE_CONFIRMED",
        now_utc=now_utc,
        features=features,
        emit=emit,
    )
    if not catdoc.content_text or not catdoc.content_text.strip():
        return False  # nothing readable extracted — don't catalog an empty fiche

    analysis = catdoc.analysis
    now_iso = _utc_iso(now_utc)
    folder = "/".join(file_path.parent.relative_to(paths.library_root).parts)
    fiche: dict[str, Any] = {
        "name": file_path.stem,
        "document_date": analysis.document_date if analysis is not None else None,
        "series": False,
        "category_path": folder or None,
        "summary": analysis.summary if analysis is not None else None,
        "keywords": analysis.keywords if analysis is not None else [],
        "entities": analysis.entities if analysis is not None else {},
        "language": analysis.language if analysis is not None else None,
        "original_filename": file_path.name,
        "read_via": catdoc.read_via,
        "provider": analysis.provider if analysis is not None else None,
        "model": analysis.model if analysis is not None else None,
        "analyzed_at": now_iso,
        "indexed_in_place": True,  # read for search only; the file is left untouched
    }
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    repo.upsert_document(
        doc_id=str(uuid4()),
        sha256=sha256,
        current_filename=file_path.name,
        current_path=str(file_path),
        status="LIBRARY_STORED",
        updated_at_utc=now_iso,
        flow_state="LIBRARY_STORED",
        pending_decision=None,
        content_json=json.dumps(fiche, ensure_ascii=True),
    )
    emit(f"   rescan indexed (in place): {file_path.relative_to(paths.library_root)}")
    _append_action_log(
        paths, operation_id=op, action="library_file_indexed", status="success",
        message="Preserve-zone document indexed in place for search (not renamed/moved).",
        now_utc=now_utc, path_after=str(file_path), features=features,
    )
    return True


def _ingest_new_library_file(
    paths: RuntimePaths,
    file_path: Path,
    *,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> None:
    """Rescan Phase 2 — a brand-new file the user placed by hand is READ IN FULL
    (its fiche goes to the catalog, for search), gets the timestamp prefix (date
    AND time; the user's stem is kept verbatim), and — for a recurring kind — is
    descended into its `<Entity>/<Year>/` subfolder exactly like the run.

    The user's FOLDER is the anchor: rescan never re-classifies into a different
    category; it only applies the dating/series convention UNDER where the file
    already lives (a new EDF bill dropped in `Utilities/EDF/` lands in
    `Utilities/EDF/2026/`). Unreadable kinds are still timestamped and catalogued
    with an empty fiche — never sent to manual review (the user chose the spot)."""
    op = str(uuid4())
    sha256 = _file_sha256(file_path)
    dispatch = dispatch_for_filename(file_path.name)

    analysis = None
    read_via: str | None = None
    content_text: str | None = None
    if dispatch.can_dispatch:
        catdoc = _read_and_analyze(
            paths,
            queued_target=file_path,
            source=file_path,
            source_folder=file_path.parent.name,
            sha256=sha256,
            dispatch=dispatch,
            operation_id=op,
            current_state="ROUTE_CONFIRMED",
            now_utc=now_utc,
            features=features,
            emit=emit,
        )
        analysis = catdoc.analysis
        read_via = catdoc.read_via
        content_text = catdoc.content_text
    else:
        emit(f"   read: unreadable kind ({file_path.name}) — timestamped, not classified")

    # The user's location WINS: anchor at the file's current folder; only apply
    # the run's deterministic series entity/year refinement under it.
    user_route = file_path.parent.relative_to(paths.library_root).parts
    series = bool(analysis.series) if analysis is not None else False
    document_date = analysis.document_date if analysis is not None else None
    document_dt = _resolve_document_date(document_date, file_path, now_utc, media_type=dispatch.media_type)
    issuer = analysis.entities.get("issuer") if analysis is not None and isinstance(analysis.entities, dict) else None
    route_dir = _with_series_entity(
        user_route, series=series, issuer=issuer if isinstance(issuer, str) else None, library_root=paths.library_root
    )
    route_dir = _with_series_year(route_dir, series=series, year=document_dt.strftime("%Y"))

    target_dir = paths.library_root / Path(*route_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    final_name = build_timestamped_filename(file_path.name, now_utc=document_dt)
    library_target = _ensure_unique_path(target_dir / final_name)
    if library_target != file_path:
        move(str(file_path), str(library_target))
    _write_text_sidecar(library_target, read_via, content_text)
    emit(f"   rescan ingested: {file_path.name} → {library_target.relative_to(paths.library_root)}")

    now_iso = _utc_iso(now_utc)
    route_label = "/".join(route_dir)
    fiche: dict[str, Any] = {
        "name": analysis.name if analysis is not None else None,
        "document_date": document_date,
        "series": series,
        "category_path": route_label or None,
        "alternatives": analysis.alternatives if analysis is not None else [],
        "summary": analysis.summary if analysis is not None else None,
        "keywords": analysis.keywords if analysis is not None else [],
        "entities": analysis.entities if analysis is not None else {},
        "language": analysis.language if analysis is not None else None,
        "source_folder": user_route[-1] if user_route else None,
        "original_filename": file_path.name,
        "effective_date": document_dt.strftime("%Y-%m-%d"),
        "read_via": read_via,
        "provider": analysis.provider if analysis is not None else None,
        "model": analysis.model if analysis is not None else None,
        "analyzed_at": now_iso,
        "hand_placed": True,
    }
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    repo.upsert_document(
        doc_id=str(uuid4()),
        sha256=sha256,
        current_filename=library_target.name,
        current_path=str(library_target),
        status="LIBRARY_STORED",
        updated_at_utc=now_iso,
        flow_state="LIBRARY_STORED",
        pending_decision=None,
        content_json=json.dumps(fiche, ensure_ascii=True),
    )
    _append_action_log(
        paths, operation_id=op, action="library_file_ingested", status="success",
        message="Hand-placed new file read in full and timestamped (rescan Phase 2).",
        now_utc=now_utc, path_before=str(file_path), path_after=str(library_target),
        extra_fields={"target_route": route_label, "series": series}, features=features,
    )
    _sync_to_mirror(paths, operation_id=op, library_file=library_target, now_utc=now_utc, features=features)


def _fiche_effective_dt(content_json: Any, fallback_path: Path, now_utc: datetime | None) -> datetime:
    """The date to (re)build a file's timestamp prefix from, taken from its stored
    fiche (no AI): the document's effective/own date, else the file's mtime, else
    now. Used by rescan to RE-DATE a hand-named file consistently with the run."""
    fiche: dict[str, Any] = {}
    if content_json:
        try:
            parsed = json.loads(content_json)
            fiche = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            fiche = {}
    for key in ("effective_date", "document_date"):
        raw = fiche.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    try:
        return datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return now_utc or datetime.now(timezone.utc)


def _ensure_timestamp_prefix(path: Path, dt: datetime) -> Path:
    """Make sure a library file carries the app's timestamp prefix — the
    horodatage is OWNED BY THE APP (spec/backlog): any file lacking it gets one
    (the user's stem is kept). A file that already carries a valid prefix is left
    untouched (never re-dated). Renames on disk and returns the final path."""
    if has_timestamp_prefix(path.name):
        return path
    target = _ensure_unique_path(path.parent / build_timestamped_filename(path.name, now_utc=dt))
    move(str(path), str(target))
    return target


def enrich_keywords(
    paths: RuntimePaths, *, force: bool = False,
    now_utc: datetime | None = None, emit: ProgressFn = lambda _m: None
) -> dict[str, int]:
    """Migration: add each filed document's keywords in English + the user's
    language (one AI call per document), so EXISTING fiches become searchable
    cross-language like newly filed ones. Idempotent — a document already enriched
    is skipped, so re-runs are cheap; pass `force=True` to re-process every
    document anyway (e.g. to refresh relevance with a better model). A no-op when
    the language is English (the catalog's base) or no AI chain is configured."""
    counts = {"enriched": 0, "skipped": 0, "failed": 0}
    language = get_user_language(paths)
    if language == "en":
        emit("Language is English — keywords are already in the catalog's base language; nothing to do.")
        return counts

    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for doc in repo.list_documents():
        if doc.get("status") != "LIBRARY_STORED" or not doc.get("content_json"):
            continue
        try:
            fiche = json.loads(str(doc["content_json"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(fiche, dict):
            continue
        if fiche.get("keywords_enriched") and not force:
            counts["skipped"] += 1
            continue
        pending.append((doc, fiche))

    if len(pending) >= LARGE_BATCH_WARN:
        emit(f"   ⚠ {len(pending)} documents to enrich — this uses AI (API cost, or local CPU/GPU).")

    op = str(uuid4())
    now_iso = _utc_iso(now_utc)
    features = load_feature_settings(paths)["features"]
    for doc, fiche in pending:
        existing = fiche.get("keywords") if isinstance(fiche.get("keywords"), list) else []
        added = translate_keywords(existing, language=language, summary=fiche.get("summary"))
        if not added:
            counts["failed"] += 1
            continue
        fiche["keywords"] = list(dict.fromkeys([*existing, *added]))
        fiche["keywords_enriched"] = True
        repo.upsert_document(
            doc_id=str(doc["doc_id"]), sha256=str(doc["sha256"]),
            current_filename=str(doc["current_filename"]), current_path=str(doc["current_path"]),
            status=str(doc["status"]), updated_at_utc=now_iso,
            flow_state=doc.get("flow_state"), pending_decision=None,
            content_json=json.dumps(fiche, ensure_ascii=True),
        )
        counts["enriched"] += 1
        emit(f"   enriched: {fiche.get('name') or doc['current_filename']}")
        _append_action_log(
            paths, operation_id=op, action="keywords_enriched", status="success",
            message=f"Keywords translated to English + {language}.",
            now_utc=now_utc, path_before=str(doc["current_path"]),
            extra_fields={"added": len(added)}, features=features,
        )

    if counts["enriched"]:
        _write_catalog_snapshot(paths, repo, now_utc, features=features)
    return counts


def run_rescan(
    paths: RuntimePaths,
    *,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> dict[str, int]:
    """Secretary sync: follow hand reorganization of the library into the catalog.

    Phase 1 (no AI): moves/renames (incl. whole folders) repoint the catalog,
    deletions become DELETED rows, deliberate duplicates are catalogued, deleted
    content re-deposited is revived. Phase 2: a brand-new hand-placed file is read
    in full and (for a series) descended into its year subfolder. The HORODATAGE is
    the app's: any moved/re-added/duplicate file lacking the timestamp prefix gets
    one (from its fiche date, keeping the user's stem); a file that already carries
    a valid prefix is never re-dated. A PRESERVE ZONE (a VCS repository, or an
    Archive folder) is left untouched as a unit, but its readable documents are
    INDEXED in place into the catalog for search. The catalogued NAME also follows
    your filename — renaming a file by hand syncs the fiche's display name (no AI),
    so search shows the name you chose. Every action is logged."""
    from procrafiler.rescan import reconcile, walk_indexable_files, walk_library_files

    counts = {"moved": 0, "readded": 0, "duplicates": 0, "deleted": 0, "new": 0, "indexed": 0, "renamed": 0}
    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    rows = repo.list_documents()
    plan = reconcile(walk_library_files(paths.library_root), rows, _file_sha256)
    # Preserve-zone docs (VCS repos + Archive folders) not yet catalogued → index.
    known_paths = {str(r.get("current_path")) for r in rows}
    repo_to_index = [p for p in walk_indexable_files(paths.library_root) if str(p) not in known_paths]

    now_iso = _utc_iso(now_utc)
    op = str(uuid4())
    root = paths.library_root

    def _rel(path: Any) -> str:
        try:
            return str(Path(str(path)).relative_to(root))
        except ValueError:
            return str(path)

    for row, new_path in plan.moved:
        old_path = str(row.get("current_path"))
        final_path = _ensure_timestamp_prefix(new_path, _fiche_effective_dt(row.get("content_json"), new_path, now_utc))
        _move_text_sidecar(Path(old_path), final_path)  # the hidden text copy follows its document
        _move_mirror_copy(  # the mirror replica follows the move too, staying path-faithful
            paths, Path(old_path), final_path, operation_id=op, now_utc=now_utc, features=features,
        )
        repo.upsert_document(
            doc_id=str(row["doc_id"]), sha256=str(row["sha256"]),
            current_filename=final_path.name, current_path=str(final_path),
            status=str(row.get("status") or "LIBRARY_STORED"), updated_at_utc=now_iso,
            flow_state=row.get("flow_state"), pending_decision=None,
            content_json=row.get("content_json"),
        )
        counts["moved"] += 1
        emit(f"   rescan moved: {_rel(old_path)} → {_rel(final_path)}")
        _append_action_log(
            paths, operation_id=op, action="library_file_moved", status="success",
            message="File moved/renamed by hand; catalog path updated, timestamp prefix ensured (no AI).",
            now_utc=now_utc, path_before=old_path, path_after=str(final_path), features=features,
        )

    for row, new_path in plan.readded:
        final_path = _ensure_timestamp_prefix(new_path, _fiche_effective_dt(row.get("content_json"), new_path, now_utc))
        repo.upsert_document(
            doc_id=str(row["doc_id"]), sha256=str(row["sha256"]),
            current_filename=final_path.name, current_path=str(final_path),
            status="LIBRARY_STORED", updated_at_utc=now_iso,
            flow_state=row.get("flow_state"), pending_decision=None,
            content_json=row.get("content_json"),
        )
        counts["readded"] += 1
        emit(f"   rescan re-added: {_rel(final_path)}")
        _append_action_log(
            paths, operation_id=op, action="library_file_readded", status="success",
            message="Previously deleted content re-deposited by hand; catalog row revived, prefix ensured (no AI).",
            now_utc=now_utc, path_after=str(final_path), features=features,
        )

    for copy_path, original in plan.duplicates:
        # A duplicate is still never deduplicated / symlinked / reorganized — but
        # the horodatage is the app's, so it gets the timestamp prefix like any
        # library file (from the original's fiche date).
        final_path = _ensure_timestamp_prefix(copy_path, _fiche_effective_dt(original.get("content_json"), copy_path, now_utc))
        repo.upsert_document(
            doc_id=str(uuid4()), sha256=str(original["sha256"]),
            current_filename=final_path.name, current_path=str(final_path),
            status="LIBRARY_STORED", updated_at_utc=now_iso,
            flow_state=original.get("flow_state"), pending_decision=None,
            content_json=original.get("content_json"),
        )
        counts["duplicates"] += 1
        emit(f"   rescan duplicate: {_rel(final_path)} (copy of {_rel(original.get('current_path'))})")
        _append_action_log(
            paths, operation_id=op, action="library_duplicate_detected", status="success",
            message=f"Hand-placed duplicate of {original.get('current_path')}; catalogued + timestamped, not deduplicated.",
            now_utc=now_utc, path_after=str(final_path), features=features,
            extra_fields={"duplicate_of": str(original.get("current_path"))},
        )

    deletion_mode = get_deletion_mode(paths)
    # Prune deleted content from the persistent search index too (so a purged /
    # tombstoned document's body text does not linger). Only touch it if it exists.
    body_index = None
    if plan.deleted and paths.search_index_file.exists():
        body_index = BodyTextIndex(paths.search_index_file)
        body_index.init_schema()
    for row in plan.deleted:
        old_path = str(row.get("current_path"))
        sha = str(row["sha256"])
        # Always quarantine the mirror copy + both hidden text sidecars to their
        # own trash, and always keep an action-log trace. How the catalog row is
        # handled depends on the deletion mode:
        #   tombstone (default) — reduce to id + hash + date (re-deposit recognised);
        #   purge — drop the row entirely (nothing of the document remains).
        _trash_deleted_artifacts(paths, Path(old_path), operation_id=op, now_utc=now_utc, features=features)
        if deletion_mode == "purge":
            repo.purge_document(str(row["doc_id"]))
            message = "File deleted from the library by hand; catalog row purged (no id/hash/fiche kept)."
        else:
            repo.tombstone_document(str(row["doc_id"]), sha256=sha, deleted_at=now_iso)
            message = "File deleted from the library by hand; catalog row reduced to a tombstone (id + hash + date)."
        if body_index is not None and not repo.has_live_sha256(sha):
            body_index.delete(sha)
        counts["deleted"] += 1
        _append_action_log(
            paths, operation_id=op, action="library_file_deleted", status="success",
            message=message, now_utc=now_utc, path_before=old_path,
            extra_fields={"deletion_mode": deletion_mode}, features=features,
        )

    if len(plan.new_files) >= LARGE_BATCH_WARN:
        emit(
            f"   ⚠ {len(plan.new_files)} new hand-placed files to read & catalog — "
            "this may take a while and use AI (API cost, or local CPU/GPU)."
        )
    for new_path in plan.new_files:
        try:
            _ingest_new_library_file(paths, new_path, now_utc=now_utc, features=features, emit=emit)
            counts["new"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the sync
            emit(f"   ⚠ rescan ingest skipped ({new_path.name}): {exc}")
            _append_action_log(
                paths, operation_id=op, action="library_ingest_error", status="error",
                message=f"Failed to ingest hand-placed file: {exc}",
                now_utc=now_utc, path_before=str(new_path), features=features,
            )

    # Index-only pass: read preserve-zone documents (repos + Archive) into the
    # catalog for search, never touching the files (no rename/move/date).
    if len(repo_to_index) >= LARGE_BATCH_WARN:
        emit(
            f"   ⚠ {len(repo_to_index)} preserved documents to index for search — "
            "this may take a while and use AI (API cost, or local CPU/GPU)."
        )
    for repo_file in repo_to_index:
        try:
            if _index_preserve_file_in_place(paths, repo_file, now_utc=now_utc, features=features, emit=emit):
                counts["indexed"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the sync
            emit(f"   ⚠ rescan index skipped ({repo_file.name}): {exc}")
            _append_action_log(
                paths, operation_id=op, action="library_index_error", status="error",
                message=f"Failed to index repo file: {exc}",
                now_utc=now_utc, path_before=str(repo_file), features=features,
            )

    # Name sync: your FILENAME is authoritative for the catalogued name too. When
    # you rename a file by hand, the fiche's display name follows it (no AI), so
    # search shows the name you chose, not the AI's original one. A no-op for files
    # the app named (the on-disk stem already equals the fiche name).
    for row in repo.list_documents():
        if row.get("status") != "LIBRARY_STORED":
            continue
        content_json = row.get("content_json")
        if not content_json:
            continue
        try:
            fiche = json.loads(content_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(fiche, dict):
            continue
        stem = _strip_ts_prefix(Path(str(row.get("current_filename") or "")).stem)
        if stem and fiche.get("name") != stem:
            fiche["name"] = stem
            repo.upsert_document(
                doc_id=str(row["doc_id"]), sha256=str(row["sha256"]),
                current_filename=str(row["current_filename"]), current_path=str(row["current_path"]),
                status=str(row["status"]), updated_at_utc=now_iso,
                flow_state=row.get("flow_state"), pending_decision=None,
                content_json=json.dumps(fiche, ensure_ascii=True),
            )
            counts["renamed"] += 1
            emit(f"   rescan name synced: {_rel(row.get('current_path'))} → \"{stem}\"")

    if any(counts.values()):
        _write_catalog_snapshot(paths, repo, now_utc, features=features)
    return counts


def _heal_double_nestings(
    paths: RuntimePaths,
    *,
    now_utc: datetime | None,
    features: dict[str, bool],
    emit: ProgressFn,
) -> int:
    """Self-healing step run at the END of every batch: collapse any accidental
    double-nesting (`…/X/X` → `…/X`) the grouping may have produced, and keep the
    catalog in sync by repointing each moved file's path. No user action needed —
    this is automatic, not a command to remember.

    Only consecutive identical path segments are merged; folders that merely share
    a name in different places are never touched (see `collapse_nesting`)."""
    from procrafiler.collapse_nesting import collapse_double_nestings

    report = collapse_double_nestings(paths.library_root, apply=True)
    if not report.redundant_dirs:
        return 0

    repo = CatalogRepository(paths.catalog_db_file)
    repo.init_schema()
    now_iso = _utc_iso(now_utc)
    root = paths.library_root
    # A move's (src, dst) may be a FILE or a whole DIRECTORY (when a clean subtree
    # was relocated wholesale). Repoint every catalog record at OR under src.
    for src, dst in report.moves:
        for record in repo.list_documents():
            current = record.get("current_path")
            if not current:
                continue
            try:
                rel = Path(current).relative_to(src)
            except ValueError:
                continue
            new_path = dst if rel == Path(".") else dst / rel
            repo.upsert_document(
                doc_id=str(record["doc_id"]),
                sha256=str(record["sha256"]),
                current_filename=new_path.name,
                current_path=str(new_path),
                status=str(record["status"]),
                updated_at_utc=now_iso,
                flow_state=record.get("flow_state"),
                pending_decision=None,
                content_json=record.get("content_json"),
            )
        emit(f"   healed nesting: {src.relative_to(root)} → {dst.relative_to(root)}")
    for src, dst in report.conflicts:
        emit(f"   ⚠ nesting collapse skipped (name collision): {src.relative_to(root)}")
    _write_catalog_snapshot(paths, repo, now_utc, features=features)
    return len(report.redundant_dirs)


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
        "collapsed_nestings": 0,
        "rescan_moved": 0,
        "rescan_new": 0,
        "total": 0,
    }

    if dry_run:
        repo = CatalogRepository(paths.catalog_db_file)
        repo.init_schema()
        known_hashes = {doc["sha256"] for doc in repo.list_documents() if doc.get("status") != "DELETED"}
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

    # Pure-secretary sync FIRST: follow any hand reorganization of the library
    # into the catalog before the AI makes new decisions. Never aborts the batch.
    try:
        rescan_counts = run_rescan(paths, now_utc=now_utc, features=features, emit=emit)
        summary["rescan_moved"] = rescan_counts["moved"]
        summary["rescan_new"] = rescan_counts["new"]
    except Exception as exc:  # noqa: BLE001 — rescan must never fail the batch
        emit(f"   ⚠ rescan skipped: {exc}")

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

    inbox_total = sum(len(members) for _, members in work_sets)
    if inbox_total >= LARGE_BATCH_WARN:
        emit(
            f"   ⚠ {inbox_total} files in the Inbox to read & classify — "
            "this may take a while and use AI (API cost, or local CPU/GPU)."
        )

    organize_chain = task_chain_from_env("ORGANIZE")
    max_depth = load_runtime_policy(paths).taxonomy_max_depth
    base_categories = [category_label(c) for c in classifiable_categories()]
    user_context = load_user_context()
    run_seen: set[str] = set()
    # Run-invariant bookkeeping (spec §1.2): library paths THIS run created, and
    # the symlinks it left (target → link). A location born during the run is
    # not a pre-run reference — regrouping from it leaves no symlink — and a
    # symlink whose file moves again is retargeted, never left dangling.
    run_placed: set[str] = set()
    run_symlinks: dict[str, Path] = {}

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
                # 3a — set when grouping aligns this new file's name to the
                # populated series it joins (so siblings match); None otherwise.
                regroup_name: str | None = None

                # M2+M3 — singleton-only grouping: show the AI the existing files
                # along the candidate branches; it may confirm, or propose a DEEPER
                # shared series/affair subfolder and pull related existing files
                # down into it. Run-invariant locks (spec §1.2): the proposed path
                # is honored only if it CREUSES (strict descendant of a candidate
                # branch — G3); existing files only ever move DEEPER (G4, inside
                # _regroup_existing_file). Skipped for folder-sets (the organizer
                # owns them), pending decisions, manual review, no-analysis files.
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
                            # C (run 12): the grouping may only CONFIRM or DEEPEN the file's OWN
                            # analysis route — to unite it with an existing series/affair AT or
                            # UNDER it — never relocate it to a sibling or a different subject.
                            # Honour the proposal only when route_dir is a prefix of it (equal =
                            # confirm + pull existing files down; longer = deepen). This stops the
                            # grouping from overriding a good classification (e.g. Hobbies/Musique
                            # → Hobbies/Fougeres) and from reforming magnets, while keeping series
                            # uniting intact.
                            own = tuple(route_dir)
                            within_own = (
                                validated_gp is not None
                                and len(validated_gp) >= len(own)
                                and tuple(validated_gp[: len(own)]) == own
                            )
                            if validated_gp is not None and within_own:
                                route_dir = validated_gp
                                dest_dir = paths.library_root / Path(*validated_gp)
                                if grouping.name:
                                    regroup_name = grouping.name
                                for existing_ref in grouping.group_with:
                                    ok = _regroup_existing_file(
                                        paths,
                                        existing_ref,
                                        candidate_branches,
                                        resolved_dirs,
                                        dest_dir,
                                        operation_id=catdoc.operation_id,
                                        now_utc=now_utc,
                                        features=features,
                                        emit=emit,
                                        run_placed=run_placed,
                                        run_symlinks=run_symlinks,
                                        series_year=bool(catdoc.analysis.series),
                                    )
                                    if ok:
                                        summary["regrouped"] += 1
                            elif validated_gp is not None:
                                # The model proposed a branch root, a sibling, or a
                                # different subject — not a subfolder UNDER the file's
                                # own route. Keep the analysis classification untouched.
                                emit("   grouping ignored (must deepen the file's own folder)")

                result = _file_cataloged(
                    paths,
                    catdoc,
                    route_dir=route_dir,
                    pending_options=pending_options,
                    pending_reason=pending_reason,
                    now_utc=now_utc,
                    features=features,
                    emit=emit,
                    override_name=regroup_name,
                )
            except Exception as exc:  # noqa: BLE001
                _record_error(exc)
                continue
            if result.library_path:
                run_placed.add(result.library_path)
            _tally(result, organized=used_organize and result.flow_state == "LIBRARY_STORED")

    # Tidy up: drop the now-empty Inbox subfolders the processed files left behind.
    _prune_empty_inbox_dirs(paths.inbox_dir)

    # Self-heal: collapse any double-nesting this run's grouping may have produced.
    try:
        summary["collapsed_nestings"] = _heal_double_nestings(
            paths, now_utc=now_utc, features=features, emit=emit
        )
    except Exception as exc:  # noqa: BLE001 — healing must never fail the batch
        emit(f"   ⚠ nesting self-heal skipped: {exc}")

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
