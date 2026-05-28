<!-- @format -->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Added

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

### Changed

- `procrafiler feature-set actions_log|catalog_snapshot|mirror_sync` now actually toggles runtime behavior. Previously the flags were stored in `settings.json` and displayed by `procrafiler status` but never consulted by the pipeline — turning a flag off was a silent no-op.
  - `actions_log` off → no JSON lines written to `actions_log.jsonl`.
  - `catalog_snapshot` off → `catalog_snapshot.json` is no longer rewritten on each operation.
  - `mirror_sync` off → no mirror copy is performed; a single `mirror_sync_skipped` event is logged (when `actions_log` is on).
- `cmd_purge_mirror_trash` in the CLI now honors `actions_log` when emitting its summary event, matching pipeline behavior.
- `process_next_inbox_file` walks the 14-state flow machine explicitly. Before the audit the spec listed 14 states but the pipeline never visited any of them — it jumped straight from "file detected" to a final string. Every checkpoint now calls `validate_transition`, raising loudly if a future change tries an illegal jump.
- CLI commands `process-once`, `process-all`, and `purge-mirror-trash` acquire a runtime lock before touching state. Read-only commands (`status`, `features`, `policy-effective`, `feature-set`, `init-layout`) stay lock-free.

### Fixed

- `tests/test_cli.py::test_purge_mirror_trash_cli` no longer drifts: it pins `PROCRAFILER_FAKE_NOW` so the CLI uses the same reference timestamp the test fabricates `mtime`s relative to. Previously the test silently broke as the real-world clock moved past the test's hand-crafted 2026-04-02 reference.

### Security

- `.gitignore` now blocks `*.env` runtime files (with `!.env.example` to keep the template tracked), `*.key`/`*.pem` private keys, and the KDE `.directory` desktop artifact.
- `scripts/install.sh` creates `procrafiler.env` and `install-meta.env` under a `0077` umask and re-enforces `0600` (user mode) or `0640` (system mode) on every install run, including upgrades from versions that did not protect the files. Closes a path where a shared system could expose `MISTRAL_API_KEY` to any local user.
- `scripts/update.sh` no longer `source`s `install-meta.env` as shell. The previous behavior allowed any tampered metadata file to execute arbitrary commands, including as root in system mode. Metadata is now parsed key-by-key with a dedicated reader that only extracts known keys (`REPO_ROOT`, `VENV_DIR`).
