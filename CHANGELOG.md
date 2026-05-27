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

### Changed

- `procrafiler feature-set actions_log|catalog_snapshot|mirror_sync` now actually toggles runtime behavior. Previously the flags were stored in `settings.json` and displayed by `procrafiler status` but never consulted by the pipeline — turning a flag off was a silent no-op.
  - `actions_log` off → no JSON lines written to `actions_log.jsonl`.
  - `catalog_snapshot` off → `catalog_snapshot.json` is no longer rewritten on each operation.
  - `mirror_sync` off → no mirror copy is performed; a single `mirror_sync_skipped` event is logged (when `actions_log` is on).
- `cmd_purge_mirror_trash` in the CLI now honors `actions_log` when emitting its summary event, matching pipeline behavior.

### Fixed

- `tests/test_cli.py::test_purge_mirror_trash_cli` no longer drifts: it pins `PROCRAFILER_FAKE_NOW` so the CLI uses the same reference timestamp the test fabricates `mtime`s relative to. Previously the test silently broke as the real-world clock moved past the test's hand-crafted 2026-04-02 reference.

### Security

- `.gitignore` now blocks `*.env` runtime files (with `!.env.example` to keep the template tracked), `*.key`/`*.pem` private keys, and the KDE `.directory` desktop artifact.
- `scripts/install.sh` creates `procrafiler.env` and `install-meta.env` under a `0077` umask and re-enforces `0600` (user mode) or `0640` (system mode) on every install run, including upgrades from versions that did not protect the files. Closes a path where a shared system could expose `MISTRAL_API_KEY` to any local user.
- `scripts/update.sh` no longer `source`s `install-meta.env` as shell. The previous behavior allowed any tampered metadata file to execute arbitrary commands, including as root in system mode. Metadata is now parsed key-by-key with a dedicated reader that only extracts known keys (`REPO_ROOT`, `VENV_DIR`).
