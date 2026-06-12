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
- An AI **reads the file content**, and **from that reading** the system derives the new name, the destination category, **and** a searchable content record.
- **What the AI reads is remembered, not thrown away.** The summary, keywords, and key data extracted from each document are persisted in the catalog (§4.1), so a file is read once and never re-read for search or reorganization.
- The extension is a **technical dispatch signal only**: it selects which capability reads the file (see §9). It never determines the name or the category.
- The AI decides name and category; guardrails (§7) govern the file operations, and any uncertain AI outcome is sent to manual review.

### 1.2 Action boundary

ProcraFiler only ever acts on files placed in its drop folder (`Inbox`, the "vrac"). It must never read from or write to any other location on disk, except the folders it created itself (library, mirror, trash, application state). The rest of the user's disk is out of scope and untouchable.

Within the library, a `run` is **monotonic**: it may create folders, place new files, and **deepen** an already-filed file (move it into a strictly more specific subfolder of where it already sits — e.g. into a series/affair folder it belongs to). It may never flatten, move a file up, or move it across branches. The Inbox is the only door through which the AI makes filing decisions; anything beyond deepening belongs to the user's own hand in the library (which the future `rescan` follows without judging).

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

ProcraFiler keeps three artifacts in its application state directory:

- `actions_log.jsonl` — append-only **operational log**: every move, trash, and processing step, recorded before/after.
- `catalog.db` — SQLite, the **source of truth**. One record per document, holding **both** its filesystem identity/lifecycle **and** the content metadata read from it (see §4.1). For code and AI, this is the queryable form.
- `catalog_snapshot.json` — a **human-readable JSON mirror** of `catalog.db`, kept synchronized and repaired on startup if a mismatch is detected.

A read-only **HTML** view rendered from the JSON snapshot is planned for later; it is **not** part of this MVP.

### 4.1 Catalog record (document fiche)

The catalog is also the **content metadata store**. When a document is read, the understanding gained from that reading is persisted on its record, so the file never has to be re-read for search or reorganization. Each record holds:

- **Identity / lifecycle:** `doc_id` (stable UUID — survives renames and moves), `sha256`, `current_filename`, `current_path`, `status` / `flow_state`, `updated_at_utc`.
- **Content metadata** (produced by the analysis step, §9):
  - `name` — the AI-derived descriptive stem used for the filename. A generalist rule (no per-type hardcoding): name by the document's most distinctive **entity** (person, organization, subject) — a CV by the person's name, a bill by the issuer — never by its file type or format.
  - `document_date` — the date found inside the content (or null).
  - `category_path` — the chosen destination, plus the `alternatives` considered.
  - `summary` — a short abstract of what the document is.
  - `keywords` — terms for later search/sort.
  - `entities` — structured key data, **extensible** (e.g. issuer, document type, amounts, references).
  - `language` (optional).
- **Grouping / dating signals:**
  - `source_folder` — the Inbox subfolder the file was dropped in (e.g. `Water-Damage`; null at the Inbox root). A signal for the organize phase: files dropped together are a candidate set — a hint, not ground truth.
  - `effective_date` — the real date the file was filed under (`YYYY-MM-DD`), from the date cascade: **EXIF capture date for photos** → the AI's content date → the file's mtime → processing time. EXIF comes first for images because it is hard metadata and sidesteps vision date-hallucination, and it makes photos taken the same day group naturally.
- **Provenance:** `read_via` (text / ocr / vision), `provider`, `model`, `analyzed_at`.

`doc_id` is the stable key: search and `reorganize` operate on these records, **not** on the files themselves. When the analysis step cannot run (no chain, all providers failed), the identity/lifecycle fields are still written and the content-metadata fields are left empty.

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
- `DECISION_PENDING` (catalog status: the AI was unsure but offered options; the file waits in the decisions queue until resolved by `review`, and is not mirrored until then)
- `INBOX_TRASH_PENDING_MANUAL`
- `ERROR_RETRYABLE`
- `ERROR_BLOCKING`

## 9. IA Architecture Policy

Reading the content is the foundation: the name, the category, and all content metadata are derived from it. The extension and the destination category are two independent decisions and must never be conflated.

- The file extension is a **technical dispatch signal only**: it selects which processing capability reads the file (PDF extraction, OCR, image analysis, plain-text reading). It never determines the name or the destination category.
- Specialized **reading** capabilities turn the bytes into text: local text/PDF extraction, OCR for scanned PDFs, vision for images. This step is the prerequisite for everything downstream.
- A **single analysis step** then consumes that text and returns the document's full fiche in **one AI call** — the descriptive **name**, the **document date**, the destination **category** (+ alternatives), a **summary**, **keywords**, and structured **entities** (§4.1). Naming and classification are **not** separate passes: one read, one analysis, one complete record. The catalog metadata thus **rides along** in the analysis response — no extra AI call is needed to make documents searchable.
- The analysis derives everything **from the content** — never from the original filename or the extension.
- An optional AI control pass (`SUPERVISOR`) reviews ambiguous outputs only.
- Capability-level backend choice (local or API), with fallback and retries.
- AI never performs irreversible actions; uncertain outcomes are sent to the decisions queue or manual review (§10).

Build order implication: the content-reading capabilities (OCR, PDF extraction, image analysis) come first, because the analysis step depends on the text they produce.

## 10. Taxonomy Policy

- Most folder architecture is predefined as a policy, even if not materialized on disk yet.
- Folder structure is materialized lazily when first used.
- Total depth is bounded by a configurable safety cap (policy `taxonomy.max_depth`).
- Folder move/rename operations require user confirmation.
- New root branch creation requires user confirmation.

Extension dispatch vs. classification:

- The extension is a technical dispatch signal only: it selects which AI capability reads the file. It MUST NOT map to a destination category.
- The destination category is always decided by AI classification from the file content, among the base branches below.
- Unknown or missing extensions cannot be dispatched to a reader and are flagged for manual review with alert logs.
- When AI classification is uncertain but can still propose plausible folders, the file enters the **decisions queue** (`DECISION_PENDING`) with those options and waits for the user to choose via `review`; when it cannot even propose options, it is flagged for plain manual review.
- New root branch creation is performed by the user during `review` (the AI may never create a top-level category).
- Filename conflicts are resolved with deterministic numeric suffixes.

The base tree is organized by life **context** (Personal / Work), then by **subject** — never by file format. Names are English (the universal architecture); a user's own inbox folder names stay in their language and are only a hint. Few top-level entries + a clear binary at each level keep it navigable for screen-reader / ADHD users and legible for the AI. Cross-cutting axes (theme, admin-type, work/personal) live in the fiche (§4.1) + soft-links, not by duplicating folders. The base tree (the AI creates anything finer — `Clients/<name>`, `Insurance/Water-Damage-2025`, `Personal/Trip-Spain-2025` — which is NOT base):

```text
Personal/
    Administrative/   Identity  Taxes  Banking  Insurance  Health  Housing  Energy  Telecom  Vehicle
    Education/
    Hobbies/
Work/
    Employment/   Administrative  Payslips
    Business/     Administrative  Invoices  Expenses  Clients
Manual_Review/
```

- No format buckets: a sinistre photo → its subject (Insurance evidence); a holiday photo → its event (`Personal/Trip-Spain-2025`). The format only selects the reader.
- `Manual_Review` is the safe catch-all (uncertain / unreadable). The taxonomy is a sane default; making it user-editable is a planned enhancement.

AI analysis policy (MVP baseline):

- The analysis step returns a **single JSON object** carrying the document fiche (§4.1), at least: `{"name": "...", "date": "YYYY-MM-DD"|null, "category_path": "...", "alternatives": ["..."], "summary": "...", "keywords": ["..."], "entities": {…}}`.
- Parsing removes wrapper text before/after the JSON object when present.
- Provider chain format is `provider:model,provider:model,...`.
- Retry uses exponential backoff per provider attempt.
- If all providers fail: deterministic fallback naming is mandatory (filename stem), the file routes to manual review, and the content-metadata fields are left empty.
- A confident `category_path` files the document; no confident path but plausible `alternatives` sends it to the decisions queue (§10, `DECISION_PENDING`); neither → plain manual review.
- Sequential queue processing remains single-file-at-a-time.

AI provider selection model (MVP):

- AI choice is user-configured per task.
- Each task has dedicated primary and fallback chain variables (e.g. `PROCRAFILER_AI_ANALYSIS_PRIMARY` / `_FALLBACK`).
- No provider is forced by default by application templates.

Task scopes include at least:

- `OCR`
- `PDF`
- `IMAGE`
- `VIDEO`
- `ANALYSIS` — unified naming + classification + content metadata in one call (replaces the former separate `NAMING` and `CLASSIFICATION` tasks).
- `SUPERVISOR`

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
