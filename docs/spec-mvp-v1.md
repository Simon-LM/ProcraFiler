<!-- @format -->

# ProcraFiler MVP Specification (v1)

## 1. Scope

ProcraFiler is a Linux-first file organization assistant.
The system automates ingestion, analysis, naming, routing, logging, and mirrored backup while keeping human control for destructive actions.

### 1.0 Problem

Files pile up with meaningless names (`scan_001.pdf`, `IMG_2024.jpg`, `document(3).pdf`). Sorting and renaming them by hand is tedious and decision-heavy, so it is endlessly procrastinated and the pile becomes unmanageable. ProcraFiler removes the friction: the user dumps everything in one drop folder and an AI reads, renames, and files each document.

### 1.1 Operating principle (IA-first)

This principle governs the whole design:

- **Every file is processed, without exception** — not only unnamed or obviously-misnamed ones. A file that already carries a name is processed too, because the name may be wrong or misleading.
- **The existing filename is never a trusted input.** It is not used to name or to classify.
- An AI **reads the file content**, and **from that reading** the system derives **both** the new name **and** the destination category.
- The extension is a **technical dispatch signal only**: it selects which capability reads the file (see §9). It never determines the name or the category.
- The AI decides name and category; guardrails (§7) govern the file operations, and any uncertain AI outcome is sent to manual review.

### 1.2 Action boundary

ProcraFiler only ever acts on files placed in its drop folder (`Inbox`, the "vrac"). It must never read from or write to any other location on disk, except the folders it created itself (library, mirror, trash, application state). The rest of the user's disk is out of scope and untouchable.

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

All managed files use a UTC timestamp prefix followed by an **AI-derived descriptive name** (from the file content — never the original filename):

`YYYY-MM-DD_HH-mm-ss__AI-Derived-Name.ext`

Example — a file dropped in as `scan_001.pdf`, once read by the AI:

`2026-04-01_22-10-06__Releve-BNP-Avril-2026.pdf`

The descriptive part comes from what the document *is*, established by reading its content, not from whatever it was called on arrival.

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

Reading the content is the foundation: both the name and the category are derived from it. The extension and the destination category are two independent decisions and must never be conflated.

- The file extension is a **technical dispatch signal only**: it selects which processing capability reads the file (PDF extraction, OCR, image analysis, plain-text reading). It never determines the name or the destination category.
- Specialized AI capabilities **read the content** (OCR, extraction, image analysis). This step is the prerequisite for everything downstream — naming and classification both consume its output.
- **AI naming derives the descriptive name from the content** — never from the original filename.
- **AI classification determines the destination category from the content** — never from the extension.
- An optional AI control pass (`SUPERVISOR`) reviews ambiguous outputs only.
- Capability-level backend choice (local or API), with fallback and retries.
- AI never performs irreversible actions; uncertain outcomes are sent to manual review.

Build order implication: the content-reading capabilities (OCR, PDF extraction, image analysis) come first, because naming and classification depend on the AI's understanding of the content.

## 10. Taxonomy Policy

- Most folder architecture is predefined as a policy, even if not materialized on disk yet.
- Folder structure is materialized lazily when first used.
- Maximum depth is 6 levels under each root.
- Folder move/rename operations require user confirmation.
- New root branch creation requires user confirmation.

Extension dispatch vs. classification:

- The extension is a technical dispatch signal only: it selects which AI capability reads the file. It MUST NOT map to a destination category.
- The destination category is always decided by AI classification from the file content, among the base branches below.
- Unknown or missing extensions cannot be dispatched to a reader and are flagged for manual review with alert logs.
- When AI classification is uncertain, the file is flagged for manual review.
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
