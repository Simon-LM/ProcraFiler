<!-- @format -->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Added

- **Documentation: anchored the project's problem and its "IA-first" principle.** `README.md` now opens with the problem it solves (files pile up with meaningless names; manual sorting is procrastinated until unmanageable) followed by a "How it works (IA-first)" section; `docs/spec-mvp-v1.md` gains §1.0 Problem, §1.1 Operating principle, and §1.2 Action boundary. The founding premise is now unambiguous: every file is processed without exception (including already-named ones, since a name may be wrong); the existing filename is never a trusted input; an AI reads the content and derives BOTH the new name AND the category from that reading; the extension is only a technical dispatch signal. The **action boundary** is stated explicitly: the app only ever acts on files in its drop folder (`Inbox`, the "vrac") and never touches anything else on disk except folders it created. Also aligned: README Goals, "Routing and Classification" and "AI Naming" sections, and spec §3 naming example (`__Original-Name.ext` → an AI-derived name like `2026-04-01_22-10-06__Releve-BNP-Avril-2026.pdf`) and §9 (naming derives from content; content-reading is the prerequisite for naming + classification).

- Initial project skeleton for ProcraFiler.
- MIT open source licensing model.
- Installation, update, and uninstall scripts for Ubuntu/Linux.
- Initial CLI entrypoint (`procrafiler`) and package structure.
- Release process documentation for changelog + tags.
- `PROCRAFILER_FAKE_NOW` environment variable: time-sensitive CLI commands now consult this when set, so tests can pin a reference timestamp and stop drifting as the real clock advances.
- `tests/test_feature_flags.py`: three end-to-end tests proving each feature flag actually changes pipeline behavior.
- `flow.validate_transition(current, next)`: enforces the state machine declared in `flow._TRANSITIONS`. Raises `InvalidTransition` on any illegal jump.
- `documents.flow_state` SQLite column: persists the final pipeline state for each catalogued document. Existing DBs are migrated in place (column added if missing on `init_schema`).
- `tests/test_state_machine.py`: end-to-end checks that the persisted `flow_state` matches the pipeline outcome on the library-store, manual-review, and duplicate paths, plus an explicit legacy-DB migration test.
- `procrafiler.runtime_lock`: cross-process advisory lock (`fcntl.flock` on `{state_root}/procrafiler.lock`) acquired by mutating CLI commands. Two parallel `procrafiler process-all` runs would previously race on the inbox; the second invocation now exits cleanly with status 75 (`EX_TEMPFAIL`) instead of clobbering files.
- `tests/test_runtime_lock.py`: covers acquire/release, denial when an external holder has the lock, recovery after release, and the CLI integration (process-once returns 75 + prints to stderr when locked).
- `procrafiler library-trash <path>` CLI command: moves a library file to `Library_Trash_Manual` (preserving the relative subdir layout) and simultaneously quarantines the matching mirror copy into `Mirror_Trash`. The library trash directory was created by `init-layout` since the start but had no command writing to it. Refuses paths outside `library_root`, missing files, and files that have no catalog entry — those need to be handled manually.
- `LIBRARY_TRASHED` flow state with transitions `LIBRARY_STORED → LIBRARY_TRASHED` and `USER_CONFIRMATION_REQUIRED → LIBRARY_TRASHED`. Terminal state, mirrors how `INBOX_TRASH_PENDING_MANUAL` works on the inbox side.
- `catalog.find_by_current_path(path)`: lookup helper to fetch a document row by its current_path, needed by `library-trash` to validate the source state before transitioning.
- `tests/test_library_trash.py`: nine tests covering the happy path (file + mirror move correctly, catalog updated), tolerance for a missing mirror copy, the four refusal paths (outside library_root, missing file, uncatalogued file, invalid transition from a non-LIBRARY_STORED state), the double-trash guard, and CLI integration (success returns 0, invalid path returns 1).
- `procrafiler doctor` CLI command + `procrafiler.doctor` module: a single read-only diagnostic that groups checks by section (Paths, Env, AI, Catalog, Concurrency) and prints a summary line. Each check produces OK / WARN / FAIL / SKIP. Exits 1 if any FAIL, else 0. Useful before the first real run to spot config issues without touching any files. Checks include: every runtime directory exists and is writable, env file loaded and 0o600/0o640 (warns on loose perms), every AI task chain (warns when no chain configured, fails when a task uses `mistral:` but `MISTRAL_API_KEY` is unset), catalog DB openable with `flow_state` column, runtime lock currently free (briefly acquires + releases).
- `tests/test_doctor.py`: 17 tests covering each check in isolation (paths fail on missing dir, env warns on loose perms, AI fails on missing key when mistral is configured, catalog warns on legacy schema, lock warns when held externally) plus the CLI integration and overall exit-code logic.
- `pipeline.reconcile_catalog_snapshot(paths)` + `procrafiler reconcile-snapshot` CLI command: implements spec §4 — compares `catalog_snapshot.json` against `catalog.db` and rewrites the snapshot from the DB when they disagree. Reasons reported: `consistent`, `missing`, `unreadable`, `content_mismatch`, `feature_disabled`. The DB is the source of truth; the snapshot is only ever rewritten in the DB→snapshot direction.
- Automatic snapshot reconciliation now runs at the start of every mutating CLI command (`process-once`, `process-all`, `library-trash`) inside the runtime lock. If the snapshot drifted (manual edit, crash mid-write, deleted file), it's repaired before the new work begins.
- `tests/test_snapshot_reconcile.py`: nine tests covering each reason path, feature-disabled skip, action-log emission on rewrite, the standalone CLI command (consistent + rewrites cases), and an end-to-end check that `process-once` repairs a corrupted snapshot before processing.
- `pipeline.ProcessResult` dataclass and internal `_process_next_inbox_file` helper carrying both the terminal `flow_state` and a `mirror_failed` flag, so the batch loop can tally mirror failures inline. `_sync_to_mirror` now returns an explicit `synced` / `skipped` / `failed` status (constants `MIRROR_SYNCED` / `MIRROR_SKIPPED` / `MIRROR_FAILED`) instead of a bare bool, letting callers tell a real failure apart from a deliberate skip.
- `tests/test_process_all_mirror_failures.py`: five tests proving the batch counts a real mirror failure inline, that a disabled mirror (skip) is not counted as a failure, and that the per-file result carries the right `mirror_failed` flag on success / failure / duplicate paths.

### Changed

- `procrafiler feature-set actions_log|catalog_snapshot|mirror_sync` now actually toggles runtime behavior. Previously the flags were stored in `settings.json` and displayed by `procrafiler status` but never consulted by the pipeline — turning a flag off was a silent no-op.
  - `actions_log` off → no JSON lines written to `actions_log.jsonl`.
  - `catalog_snapshot` off → `catalog_snapshot.json` is no longer rewritten on each operation.
  - `mirror_sync` off → no mirror copy is performed; a single `mirror_sync_skipped` event is logged (when `actions_log` is on).
- `cmd_purge_mirror_trash` in the CLI now honors `actions_log` when emitting its summary event, matching pipeline behavior.
- `process_next_inbox_file` walks the 14-state flow machine explicitly. Before the audit the spec listed 14 states but the pipeline never visited any of them — it jumped straight from "file detected" to a final string. Every checkpoint now calls `validate_transition`, raising loudly if a future change tries an illegal jump.
- CLI commands `process-once`, `process-all`, and `purge-mirror-trash` acquire a runtime lock before touching state. Read-only commands (`status`, `features`, `policy-effective`, `feature-set`, `init-layout`) stay lock-free.
- `process_all_inbox_files` no longer re-reads the entire `actions_log.jsonl` before and after every file just to count mirror failures (an O(N²) scan that grew with the log). It now reads the per-file `mirror_failed` flag returned by the pipeline and tallies inline. The public `process_next_inbox_file` keeps its string return contract unchanged.
- **Documentation: corrected the routing/classification logic** in `README.md` (Goals + new "Routing and Classification" section, formerly "Deterministic Routing") and `docs/spec-mvp-v1.md` (§9 IA Architecture Policy, §10 routing baseline). The previous wording prescribed extension→category routing ("common extensions are auto-routed to standard branches"), which conflates a technical signal (extension) with a semantic decision (category). The corrected logic states that the extension is a **technical dispatch signal only** (it selects which AI capability reads the file) and that the **destination category is always decided by AI from the file content**, never from the extension.
- **Decoupled technical dispatch from semantic classification in the code.** `taxonomy.decide_route_for_filename` (extension→category folder) is replaced by `taxonomy.dispatch_for_filename`, which returns a `DispatchDecision(media_type, reason, matched_extension)` — `media_type` is the reader class (pdf/text/office/image/video/audio/archive), never a destination. Since no AI classifier exists yet, every readable (known-extension) file is now ingested into an interim review directory (`taxonomy.INTERIM_LIBRARY_DIR` = `Revue_Manuelle`) instead of a wrongly extension-derived category like `Personnel/Documents`. Files with no/unknown extension still go to manual review in the queue (they can't even be read). The naming + mirror + catalog steps are unchanged; only the destination changed. When AI classification lands, it will replace the interim destination with a real content-derived category.

### Fixed

- `tests/test_cli.py::test_purge_mirror_trash_cli` no longer drifts: it pins `PROCRAFILER_FAKE_NOW` so the CLI uses the same reference timestamp the test fabricates `mtime`s relative to. Previously the test silently broke as the real-world clock moved past the test's hand-crafted 2026-04-02 reference.

### Security

- `.gitignore` now blocks `*.env` runtime files (with `!.env.example` to keep the template tracked), `*.key`/`*.pem` private keys, and the KDE `.directory` desktop artifact.
- `scripts/install.sh` creates `procrafiler.env` and `install-meta.env` under a `0077` umask and re-enforces `0600` (user mode) or `0640` (system mode) on every install run, including upgrades from versions that did not protect the files. Closes a path where a shared system could expose `MISTRAL_API_KEY` to any local user.
- `scripts/update.sh` no longer `source`s `install-meta.env` as shell. The previous behavior allowed any tampered metadata file to execute arbitrary commands, including as root in system mode. Metadata is now parsed key-by-key with a dedicated reader that only extracts known keys (`REPO_ROOT`, `VENV_DIR`).
