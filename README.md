<!-- @format -->

# ProcraFiler

ProcraFiler is a Linux application (Ubuntu-first) for AI-assisted file sorting and classification.
The AI acts as an analysis assistant, while decisions and actions remain controlled by explicit rules and safety guardrails.

## Publisher

- Author: Simon LM
- Company: LostInTab
- Software suite: ProcraTools
- Portfolio: [simon-lm.dev](https://simon-lm.dev)
- GitHub: [github.com/Simon-LM](https://github.com/Simon-LM)
- ProcraFiler repository: to be added once the repository is created on GitHub

## Goals

- Automatically process new files from the Downloads folder.
- Rename files with UTC timestamp prefixes.
- Classify files into a main target library by AI, from the file content. The extension only selects which AI capability reads the file; it never decides the destination category.
- Process an archive folder of unclassified files (legacy files), with duplicate detection.
- Generate per-document classification history.
- Generate complete operational logs (moves, sizes, timestamps, statuses).

## Default Folder Layout

User-facing workspace (default):

- `~/Downloads/ProcraFiler_Inbox/Inbox`
- `~/Downloads/ProcraFiler_Inbox/Queue`
- `~/Downloads/ProcraFiler_Inbox/Inbox_Trash_Manual`

Main library:

- `~/ProcraFiler_Library`
- `~/ProcraFiler_Library_Trash_Manual`

Mirrored library:

- `~/ProcraFiler_Library_Mirror`
- `~/ProcraFiler_Library_Mirror/Mirror_Trash`

Application state (default):

- `~/.local/share/procrafiler/actions_log.jsonl`
- `~/.local/share/procrafiler/catalog.db`
- `~/.local/share/procrafiler/catalog_snapshot.json`
- `~/.config/procrafiler/settings.json`
- `~/.config/procrafiler/policy.toml`

Runtime environment template:

- `.env.example`
- user install expected file: `~/.config/procrafiler/procrafiler.env`
- system install expected file: `/etc/procrafiler/procrafiler.env`

This separation keeps user files in `Downloads` while operational metadata stays in a stable app state location.

Full MVP details are documented in [docs/spec-mvp-v1.md](docs/spec-mvp-v1.md).

## Routing and Classification

ProcraFiler keeps two decisions strictly separate. Conflating them is a design error.

**1. Technical dispatch — by extension.** The file extension decides *only* which processing capability reads the file:

- `pdf` → PDF text extraction; scanned image → OCR; `jpg`/`png` → image analysis; `txt`/`md` → direct text reading; and so on.
- The extension **never** decides the destination category.
- Unknown or missing extensions cannot be dispatched to a reader and are flagged for manual review with an explicit alert in the action log.
- Name conflicts are resolved with `__1`, `__2`, ... suffixes.

**2. Semantic classification — by AI, from the content.** Once the right capability has read the file, an AI classification pass decides the destination category from the *content*, never from the extension:

- A scanned receipt saved as `.jpg` is an administrative document, not a media image — only the content can tell.
- When the AI is uncertain, the file goes to manual review. The AI never performs an irreversible action.

Base folders are semantic categories (every destination is AI-decided from content, never reached by an extension rule):

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

The media folders (`Personnel/Medias/...`) are themselves content-decided destinations: a file being an image by extension does not by itself send it there — a photographed document is classified by what it contains.

## AI Naming (MVP)

ProcraFiler can use AI-generated filename stems with provider failover.

Expected AI output format:

- JSON object with one key: `{"stem":"..."}`
- If a model returns extra text before/after JSON, ProcraFiler extracts the JSON object.
- If no valid JSON object is found, ProcraFiler falls back to deterministic naming.

- Provider chain format: `provider:model,provider:model,...`
- Split rule: split only on the first `:`
- Retry strategy: exponential backoff per provider attempt (`1s`, `2s`, `4s`, ...)
- Failover: move to next provider when retries are exhausted
- Safe fallback: keep deterministic stem if all providers fail
- No provider is forced by default. Each task is user-configured.

Environment variables:

- `PROCRAFILER_AI_NAMING_PRIMARY`
- `PROCRAFILER_AI_NAMING_FALLBACK`
- `PROCRAFILER_AI_OCR_PRIMARY`
- `PROCRAFILER_AI_OCR_FALLBACK`
- `PROCRAFILER_AI_PDF_PRIMARY`
- `PROCRAFILER_AI_PDF_FALLBACK`
- `PROCRAFILER_AI_IMAGE_PRIMARY`
- `PROCRAFILER_AI_IMAGE_FALLBACK`
- `PROCRAFILER_AI_VIDEO_PRIMARY`
- `PROCRAFILER_AI_VIDEO_FALLBACK`
- `PROCRAFILER_AI_SUPERVISOR_PRIMARY`
- `PROCRAFILER_AI_SUPERVISOR_FALLBACK`
- `PROCRAFILER_AI_CLASSIFICATION_PRIMARY`
- `PROCRAFILER_AI_CLASSIFICATION_FALLBACK`
- `PROCRAFILER_AI_TIMEOUT` / `PROCRAFILER_AI_RETRIES` (global defaults)
- `PROCRAFILER_AI_NAMING_TIMEOUT` / `PROCRAFILER_AI_NAMING_RETRIES` (task override)
- `MISTRAL_API_KEY` (required for Mistral calls)

## Feature Controls (Terminal)

Initialize folders and metadata files:

```bash
procrafiler init-layout
```

Show all paths and feature flags:

```bash
procrafiler status
procrafiler features
```

`status` also shows `env_loaded_from` so you can verify the active environment file.

Enable/disable features:

```bash
procrafiler feature-set actions_log on
procrafiler feature-set catalog_snapshot on
procrafiler feature-set mirror_sync on

procrafiler feature-set actions_log off
```

The same feature toggles can be reused later by a web UI.

## Runtime Policy (policy.toml)

ProcraFiler creates a runtime policy file at:

- `~/.config/procrafiler/policy.toml`

Default content:

```toml
[mirror]
retention_days = 30
versions_keep = 3

[taxonomy]
max_depth = 6
```

Behavior rules:

- Policy values must be positive integers.
- Invalid or missing values automatically fall back to safe defaults.
- `purge-mirror-trash` uses `mirror.retention_days` when `--days` is not provided.

Show the effective policy currently applied:

```bash
procrafiler policy-effective
```

## License

This project is released under the MIT License (permissive open source).
Commercial use is allowed, provided license and copyright notices are preserved.
See [LICENSE.md](LICENSE.md).

## Versioning, Changelog, and Tags

- Version format: SemVer (`MAJOR.MINOR.PATCH`).
- Changelog: [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog format).
- Recommended GitHub tags: `vX.Y.Z`.
- Detailed process: [docs/release.md](docs/release.md).

## Ubuntu Installation

Full guide: [docs/ubuntu-deploy.md](docs/ubuntu-deploy.md).

### Option A: local user installation (recommended for development)

```bash
./scripts/install.sh --mode user
```

This installs the `procrafiler` command into `~/.local/bin`.

### Option B: system installation (root)

```bash
sudo ./scripts/install.sh --mode system
```

By default, the binary is linked into `/usr/local/bin`.
To force `/usr/bin`, use `--prefix /usr`.

## Update

From the local Git clone on the target machine:

```bash
# user mode
./scripts/update.sh --mode user

# system mode
sudo ./scripts/update.sh --mode system
```

The script fetches the latest commits/tags and reinstalls the application in its virtual environment.

## Uninstall

```bash
./scripts/uninstall.sh --mode user
# or
sudo ./scripts/uninstall.sh --mode system
```

## Project Status

Initial skeleton is in place to start development of the classification engine, AI connectors, and safety pipelines.
