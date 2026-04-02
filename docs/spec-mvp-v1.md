<!-- @format -->

# ProcraFiler MVP Specification (v1)

## 1. Scope

ProcraFiler is a Linux-first file organization assistant.
The system automates ingestion, analysis, naming, routing, logging, and mirrored backup while keeping human control for destructive actions.

## 2. Core Folder Structure

### 2.1 Inbox workspace (inside Downloads)

- `~/Downloads/ProcraFiler_Inbox/Inbox`
- `~/Downloads/ProcraFiler_Inbox/Queue`
- `~/Downloads/ProcraFiler_Inbox/Inbox_Trash_Manual`

### 2.2 Main library

- `ProcraFiler_Library`
- `ProcraFiler_Library_Trash_Manual`

### 2.3 Synchronized mirror

- `ProcraFiler_Library_Mirror`
- `ProcraFiler_Library_Mirror/Mirror_Trash`

## 3. Naming Convention

All managed files must use a UTC timestamp prefix:

`YYYY-MM-DD_HH-mm-ss__Original-Name.ext`

Example:

`2026-04-01_22-10-06__Tax-Report-2024.pdf`

## 4. Data Artifacts

- `actions_log.jsonl` (append-only operational log)
- `catalog.db` (SQLite source of truth)
- `catalog_snapshot.json` (human-readable mirror of catalog)

`catalog_snapshot.json` must stay synchronized with SQLite and be repaired on startup if a mismatch is detected.

## 5. Duplicate Policy

- Inbox duplicates are moved to `Inbox_Trash_Manual`.
- Deletion in Inbox and main Library is always manual.
- ProcraFiler never performs direct permanent deletion in Inbox/Library.

## 6. Mirror Retention Policy

- Mirror operations are sync-first and hash-verified.
- Obsolete mirror files are quarantined in `Mirror_Trash`.
- Quarantined mirror files are purged after TTL (configurable).
- Mirror may keep only the last N versions for high-churn files.

## 7. Guardrails

- No destructive action without explicit policy allowance.
- No operation outside allowed root paths.
- Every sensitive operation is logged before/after execution.
- `synced` status requires successful hash verification.
- Uncertain AI outcomes must go to manual review.

## 8. Flow States (Main)

- `INBOX_NEW`
- `INBOX_QUEUED`
- `PROCESSING_LOCKED`
- `ANALYSIS_RUNNING`
- `DUPLICATE_CANDIDATE`
- `CLASSIFICATION_READY`
- `ROUTE_PROPOSED`
- `TAXONOMY_UPDATE_REQUIRED`
- `USER_CONFIRMATION_REQUIRED`
- `ROUTE_CONFIRMED`
- `LIBRARY_STORED`
- `INBOX_TRASH_PENDING_MANUAL`
- `ERROR_RETRYABLE`
- `ERROR_BLOCKING`

## 9. IA Architecture Policy

- Deterministic routing first (by file type and rules).
- Specialized AI capabilities second (OCR, extraction, classification, image analysis).
- Optional AI control pass for ambiguous outputs only.
- Capability-level backend choice (local or API), with fallback and retries.
- AI never performs irreversible actions.

## 10. Taxonomy Policy

- Most folder architecture is predefined as a policy, even if not materialized on disk yet.
- Folder structure is materialized lazily when first used.
- Maximum depth is 6 levels under each root.
- Folder move/rename operations require user confirmation.
- New root branch creation requires user confirmation.

MVP deterministic routing baseline:

- Routing priority is extension-first.
- Common extensions are auto-routed to standard branches.
- Unknown or missing extensions are flagged for manual review with alert logs.
- Filename conflicts are resolved with deterministic numeric suffixes.

MVP base branches include at least:

- `Personnel/Documents`
- `Professionnel/Documents`
- `Administratif`
- `Banque`
- `Telephonie`
- `Internet`
- `Personnel/Medias/Images`
- `Personnel/Medias/Videos`
- `Personnel/Medias/Audio`
- `Personnel/Archives`
- `Revue_Manuelle`

AI-assisted naming policy (MVP baseline):

- Expected model output is JSON with schema `{"stem":"..."}`.
- Parsing removes wrapper text before/after the JSON object when present.
- Naming provider chain format is `provider:model,provider:model,...`.
- Retry uses exponential backoff per provider attempt.
- If all providers fail, deterministic fallback naming is mandatory.
- Sequential queue processing remains single-file-at-a-time.

AI provider selection model (MVP):

- AI choice is user-configured per task.
- Each task has dedicated primary and fallback chain variables.
- No provider is forced by default by application templates.

Task scopes include at least:

- `NAMING`
- `OCR`
- `PDF`
- `IMAGE`
- `VIDEO`
- `SUPERVISOR`
- `CLASSIFICATION`

## 11. Runtime and Deployment

- Python + venv (no Docker for MVP)
- Bash install/update scripts
- Linux systemd user service support

## 12. Runtime Policy File

ProcraFiler uses a runtime policy file at `~/.config/procrafiler/policy.toml`.

Default policy:

```toml
[mirror]
retention_days = 30
versions_keep = 3

[taxonomy]
max_depth = 6
```

Policy constraints:

- All policy values must be positive integers.
- Invalid, missing, or unreadable values fall back to defaults.
- Mirror purge TTL defaults to `mirror.retention_days` when no CLI override is provided.

## 13. Implementation Gate

Code implementation starts only after explicit `GO CODE` from the user.
