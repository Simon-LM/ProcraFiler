<!-- @format -->

# ProcraFiler

ProcraFiler is a Linux application (Ubuntu-first) that organizes your documents by **reading them with AI**.

## The problem

Files pile up with meaningless names — `scan_001.pdf`, `IMG_2024.jpg`, `document(3).pdf`. Sorting and renaming them by hand is tedious and decision-heavy, so it gets put off indefinitely (procrastination), and the pile grows until it is unmanageable.

ProcraFiler removes that friction: you dump everything in one place and an AI does the reading, renaming, and filing for you.

## How it works (IA-first)

This is the core idea; everything else follows from it.

- You drop files into a single **drop folder** (the `Inbox`, your "vrac"). **The app only ever processes what is in there** — it never touches anything else on your disk, except the folders it created itself.
- An **AI reads each file's content** and, from that reading, **renames it** (timestamped) and **files it into a category**. The new name and the category are *outputs* of reading the content.
- The **existing filename never decides** — that is the whole point. Every file is processed, even already-named ones, because the name may be wrong. But the name is not thrown away either: it is a **strong hint** passed to the AI alongside the folder it was dropped in and the names of the files dropped with it. Its weight rises as the content gets less reliable — for a photo read by a vision model (which can misread or hallucinate), those filesystem facts are treated as **corroborating evidence** and can outweigh a vague visual description; for a text layer read mechanically, the content stays authoritative.
- The **extension** only selects *which* AI reads the file (PDF extraction, OCR, image analysis, …). It never decides the name or the category.
- A **photographed document is transcribed, not just described**: a photo goes to the vision model because it is a `.jpg`, so an invoice snapped with a phone used to come back as "an administrative document" instead of its amount and reference. The vision model is now asked whether the image is a written document, and if so the file is re-read with the OCR model. Both texts are kept — the transcription first, the visual context after — and it is the transcription that lands in the search index, so you can later search that invoice by its amount.
- **Safe by design:** the app never deletes anything (files only move to a trash folder you empty yourself), the mirror is hash-verified, and any AI doubt goes to manual review.

## Publisher

- Author: Simon LM
- Company: LostInTab
- Software suite: ProcraTools
- Portfolio: [simon-lm.dev](https://simon-lm.dev)
- GitHub: [github.com/Simon-LM](https://github.com/Simon-LM)
- ProcraFiler repository: [github.com/Simon-LM/ProcraFiler](https://github.com/Simon-LM/ProcraFiler)

## Goals

- Automatically process **every** new file dropped into the **Inbox** drop folder (the "vrac"), regardless of its current name — the app only ever processes what is in there, never the rest of your disk.
- Have an AI read each file's content, and from that reading derive a new descriptive name (under a UTC timestamp prefix) — the original filename is never reused as the basis.
- Classify files into a main target library by AI, from the file content. The extension only selects which AI capability reads the file; it never decides the name or the destination category.
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

Every file is read by an AI; its name and category are **outputs of that reading**, never derived from the original filename (see the operating principle above). On top of that, ProcraFiler keeps two decisions strictly separate. Conflating them is a design error.

**1. Technical dispatch — by extension.** The file extension decides *only* which processing capability reads the file:

- `pdf` → PDF text extraction; scanned image → OCR; `jpg`/`png` → image analysis; `txt`/`md` → direct text reading; and so on.
- The extension **never** decides the destination category.
- Unknown or missing extensions cannot be dispatched to a reader and are flagged for manual review with an explicit alert in the action log.
- Name conflicts are resolved with `__1`, `__2`, ... suffixes.

**2. Semantic classification — by AI, from the content.** Once the right capability has read the file, an AI classification pass decides the destination category from the *content*, never from the extension:

- A scanned receipt saved as `.jpg` is an administrative document, not a media image — only the content can tell.
- When the AI is uncertain, the file goes to manual review. The AI never performs an irreversible action.

The base library tree is organized by life **context** (Personal / Work) and then by **subject** — never by file format. Names are English (the universal architecture); your own inbox folder names stay in your language and are only a hint. The AI files into these and creates anything finer itself (e.g. `Clients/<name>`, `Insurance/Water-Damage-2025`, `Personal/Trip-Spain-2025`).

```text
Personal/
    Administrative/   Identity  Taxes  Banking  Insurance  Health  Housing  Utilities  Telecom  Vehicle
    Education/
    Hobbies/   Social-media   Misc
    Archive/          # your keep-as-is zone (see below) — the AI never files here
Work/
    Employment/   Administrative  Payslips
    Business/     Administrative  Invoices  Expenses  Clients  Misc
    Archive/          # your keep-as-is zone — the AI never files here
Manual_Review/    # safe catch-all (uncertain / unreadable)
```

There are **no format buckets**: a photographed receipt saved as `.jpg` is classified by what it *contains* (→ `Personal/Administrative/...`), while a holiday photo goes to its event (`Personal/Trip-Spain-2025`). The format only decides which reader extracts the content, never the destination. Cross-cutting views ("all my bills") come from the catalog fiche (keywords/entities) and soft-links, not by duplicating folders. The taxonomy is a sane default you can adapt.

**`Archive/` (Personal and Work) is your preserve zone.** Drop in anything you want kept **exactly as you arranged it** — backups, snapshots, old folders, a git repository. The AI **never files anything here on its own** (it's not a classification target — archiving is your deliberate act, and this avoids a catch-all magnet). `rescan` treats everything under `Archive/` like a git repository: it **reads the readable documents inside for search** (they enter the catalog) but **never renames, moves or reorganizes** them.

## AI Analysis (MVP)

A **single analysis call** reads the file **content** and returns the whole document fiche at once: the descriptive name, the document's date, the destination category (+ alternatives), a summary, keywords, and entities. Naming and classification are not separate passes — one read, one call, one record persisted in the catalog (so files become searchable at no extra AI cost). The descriptive name goes under a UTC timestamp prefix.

The content decides; the original filename never does. But it is **given to the AI as a hint** — together with the drop folder and the names of the files dropped alongside — because it is often accurate and it costs nothing. How much that hint weighs depends on **how the content was read**: text extracted mechanically (a `.txt` file, a PDF text layer) is literal and authoritative, so the hints only break ties; text produced by an AI reading an image (OCR, vision) is itself an interpretation that can be incomplete or invented, so the filename, folder and set-mates become **corroborating evidence** — and a specific name that clearly contradicts a vague visual description wins, or the file goes to the decisions queue rather than being guessed. If no content can be read at all and the AI is unavailable, the original stem is the deterministic fallback.

The **vision model gets those two names as well** — the file's own and its drop folder — because some photos cannot be read on their own: a close-up of green fibres is a lawn or a soaked carpet depending only on what surrounds it. Unaided, the model answers "an abstract pattern"; told where the photo came from, it commits. The names are given to it as explicitly fallible — possibly meaningless (`IMG_2024`) or plainly wrong — with one power only, **breaking a tie on an ambiguous image**, and none at all to add something the image does not show: a garden photo misnamed `facture-EDF.jpg` is still read as a garden. OCR needs no such hint, since it transcribes what is actually on the page.

Expected AI output format:

- A single JSON object: `{"name":"...", "date":"YYYY-MM-DD"|null, "category_path":"...", "alternatives":[...], "summary":"...", "keywords":[...], "entities":{...}}`
- If a model returns extra text before/after JSON, ProcraFiler extracts the JSON object.
- If no valid JSON object is found, ProcraFiler retries, then falls back to deterministic naming + manual review.

- Provider chain format: `provider:model,provider:model,...`
- Split rule: split only on the first `:`
- Retry strategy: exponential backoff per provider attempt (`1s`, `2s`, `4s`, ...)
- Failover: move to next provider when retries are exhausted
- Safe fallback: deterministic stem + manual review if all providers fail
- No provider is forced by default. Each task is user-configured.

Environment variables:

- `PROCRAFILER_AI_ANALYSIS_PRIMARY` (the unified read→name→classify→summarize call)
- `PROCRAFILER_AI_ANALYSIS_FALLBACK`
- `PROCRAFILER_AI_NAMING_PRIMARY` (set-aware pass: re-judge the names of a whole dropped folder together)
- `PROCRAFILER_AI_NAMING_FALLBACK`
- `PROCRAFILER_AI_ORGANIZE_PRIMARY` (set-aware pass: group a dropped folder into a dated affair/series)
- `PROCRAFILER_AI_ORGANIZE_FALLBACK`
- `PROCRAFILER_AI_OCR_PRIMARY` (scanned / image-only PDFs)
- `PROCRAFILER_AI_OCR_FALLBACK`
- `PROCRAFILER_AI_IMAGE_PRIMARY` (vision model for images)
- `PROCRAFILER_AI_IMAGE_FALLBACK`
- `PROCRAFILER_AI_TIMEOUT` (Mistral **API** per-call timeout, default 60 s) / `PROCRAFILER_AI_RETRIES` (global defaults)
- `PROCRAFILER_AI_LOCAL_TIMEOUT` (**local** Ollama: a *no-progress* / idle timeout, default 15 min — local calls stream, so a merely-slow model is never killed mid-generation; see [docs/ai-providers.md](docs/ai-providers.md))
- `PROCRAFILER_AI_ANALYSIS_TIMEOUT` / `PROCRAFILER_AI_ANALYSIS_RETRIES` (per-task override — `_<TASK>_TIMEOUT` overrides either the API or the local default)
- `PROCRAFILER_AI_TEMPERATURE` / `PROCRAFILER_AI_TOP_P` (Mistral sampling; **unset = the provider's own default**, the neutral baseline — set a float, e.g. `0.3`, to override globally)
- `MISTRAL_API_KEY` (required for Mistral calls)

## Commands

Processing (the core loop):

```bash
procrafiler process-once            # process one file from the Inbox
procrafiler process-once --dry-run  # simulate, mutate nothing
procrafiler process-all             # process every file currently in the Inbox
procrafiler process-all --limit 5   # try it on a handful first; never splits a dropped folder
procrafiler process-all --dry-run
procrafiler undo-run --dry-run      # what undoing the last run would put back
procrafiler undo-run                # put the last run back: documents return to the Inbox
procrafiler review                  # resolve files the AI was unsure about (the decisions queue)
procrafiler setup-context           # guided questionnaire → your context file (helps the AI file your docs)
procrafiler search <terms>          # find documents by fiche AND body text (offline, ranked; e.g. `search facture edf`)
procrafiler search-ai <terms>       # deeper search: an AI broadens your query with synonyms + translations, then searches
```

`setup-context` is a short, universal questionnaire (who you are, your work + the names that mean *your* work, your interests, your household) that writes your **context file** for you — no config to hand-edit. It only **guides** the AI (the document's content still decides), so a hobby you forgot or a project you didn't list is still handled from the content. Your answers stay on your machine (the context file is gitignored, never committed).

**Dropping a folder means something.** Files you drop loose in the Inbox are handled one by one. Files you drop **inside a folder** are read as a set, and that context is weighed when each file is named — because a name derived from one file's content alone can simply be wrong. The clearest case is a photo: a vision model describing a scene with no legible text can be confidently mistaken, while the files dropped alongside it say plainly what the set is about. So after every file of the folder has been read, a **naming pass** re-judges their names together (`PROCRAFILER_AI_NAMING_*`), before any filing. It is not a grouping mechanism — a folder may legitimately hold several unrelated subjects, and then each file keeps its own identity; coherent groupings simply tend to emerge from the context. A file it genuinely cannot settle goes to the decisions queue rather than being named on a guess. With no naming chain configured it is a no-op. And a name you simply don't like is never a problem: rename it by hand in the library and `rescan` follows your choice — your name always wins.

Files you drop **together in a subfolder** are treated as a set: after the per-file pass, `process-all` runs a **set-aware organize pass** (configurable via `PROCRAFILER_AI_ORGANIZE_*`; Mistral medium in the default profile, or a local model) that groups them into a shared **dated affair/series folder** (e.g. a water-damage claim → `…/Insurance/Degats-eaux-2025-08/`, recurring meter readings → a series folder). With no organize chain configured it's a no-op. Loose files at the Inbox root are handled individually.

**Every run tells you what it will cost before making a single call** — e.g. *"≈ 12 to 16 AI call(s) for 5 file(s): 3 image read(s), 1 PDF(s), OCR only if scanned, 5 analysis, 2 naming, 2 organize"*. No file is opened to work that out, so it is instant on a large Inbox; that is also why it is a range, and it says so — a PDF may have a text layer (free) or be a scan (one OCR call), and a photo costs a second call only if it turns out to be a photographed document. Tasks with no provider chain configured are not counted, since those calls never happen. Pair it with **`--limit N`** for a cautious first run: the limit counts files but is applied **by drop**, so a folder you dropped is never cut in half (its files are named together — half a folder would be judged against half its context). The rest waits in the Inbox for your next run.

**A run can be put back.** Every `process-all` prints a run id and tags all its actions with it, so **`procrafiler undo-run`** returns the documents that run filed to the exact Inbox subfolder they came from — files dropped together stay a set for your next attempt. It shows the plan and asks before moving anything (`--dry-run` to only look, `--run-id` for an older run, `--list` to see recent ones). It **refuses rather than guesses**: a document you have since renamed or moved by hand is reported and left strictly alone, because the catalog — not the log — is the authority on where your documents are. Nothing is deleted: mirror copies go to `Mirror_Trash`, and the only thing removed is the hidden cache of AI-extracted text, which would otherwise be ingested as a document of its own on the next run.

When the AI cannot confidently place a file but has plausible candidates, it does not guess: the file is parked in the **decisions queue** (`Manual_Review`, status `DECISION_PENDING`) and `process-all` tells you how many are waiting. `procrafiler review` walks them one by one, showing the AI's options — you pick one, type a custom path (a new subfolder, or a brand-new top-level category, which is allowed only here), or skip. Only once you resolve a file is it re-filed and mirrored.

When you reorganize the library **by hand** — rename a file, move it, or rename/move a whole folder — `process-all` first runs a **rescan** (also available standalone) that follows your changes into the catalog **without any AI**: it tracks each file by its content fingerprint, so a moved or renamed file (or every file in a renamed folder) just has its catalog path updated — never re-read, re-classified or re-named. **Your location and name always win** — the one thing the app owns is the **timestamp prefix**: any file that lacks it (e.g. one you renamed without it) gets it back from its catalogued date, keeping your name. A file you deleted by hand is recorded (and listable via `deleted-history`) — by default as a **tombstone** (id + content hash + date, so re-adding it is recognised, not re-trashed as a duplicate); set `deletion-mode purge` if you instead want **nothing** of a deleted document kept in the catalog (the deletion then survives only in the action log). A deliberate duplicate you placed is catalogued but never touched. A genuinely **new** file you drop into the library is read once (its fiche enters the catalog, for search), gets the **timestamp prefix** (your stem kept), and — if it's a recurring kind — is dated into its `…/<Entity>/<Year>/` subfolder like the run; it stays in the folder you chose. A **git repository** you drop in (a folder with a `.git`) is left **untouched as a unit** — never renamed or reorganized — but its readable documents (`.md`, `.txt`, `.pdf`…) are **indexed in place** into the catalog so they're searchable too.

`search` looks in both the **fiche** (name, keywords, entities, summary — "find by what it is") and the document's **body text** ("find by what it says"): a word that appears only inside a document surfaces it. It is **typo-tolerant** (a misspelled word falls back to its closest indexed terms — `pasisons` finds `passions` — offline, no AI) and **multilingual on the category**: a document is found by its folder in English *and* in your language (e.g. `Hobbies` by `loisirs`/`passion`), once you set `procrafiler language`. Newly filed documents also get keywords in English and your language. For the cases where exact search is still too narrow, **`search-ai`** brings in a small AI to broaden your query with synonyms and translations (English + your language) before searching — e.g. `search-ai acoustique` also finds documents indexed under `audio`/`son`/`sound`. The plain `search` stays offline and instant; `search-ai` is the opt-in deeper pass, and falls back to `search` when no AI is configured. Body text is read with no AI and no network — from the document itself for plain-text files and text-layer PDFs, and from a small **hidden sidecar** (`.<filename>.txt`) for documents whose text could only be read by AI (OCR on a scanned PDF, vision on an image): that costly text is extracted **once** and cached in the sidecar, so search reads it without ever re-OCR'ing. rescan moves the sidecar with its document and mirrors it. (A scanned page with no sidecar is still findable by its fiche.) Extracted body text is cached in a small **persistent index** (`search_index.db`, keyed by content hash) so search never re-reads a file twice — it warms itself as you search, and `procrafiler reindex` pre-builds or refreshes it in one pass (a deleted document's text is dropped from the index too).

Diagnostics and maintenance:

```bash
procrafiler status                  # paths, features, policy, deletion mode, language, search index
procrafiler --version               # the installed version (always tracks the release tag)
procrafiler doctor                  # check paths, env, AI config, catalog, lock (exit non-zero on FAIL)
procrafiler rescan                  # follow hand moves/renames/deletes in the library into the catalog (no AI)
procrafiler scrub                   # integrity check + self-heal (re-hash vs catalog; --repair); also refreshes the mirror's catalog copy
procrafiler verify-catalog          # check the catalog DB integrity; --rebuild reconstructs it from catalog_snapshot.json if corrupt/lost
procrafiler backup --to <dir>       # write a dated, self-contained backup archive (.tar.gz + .sha256); --encrypt for a passphrase-protected (AES-256-GCM) bundle
procrafiler restore --from <mirror> # disaster recovery: rebuild the library + catalog from a mirror (or --from-archive <file>)
procrafiler restore --from <mirror> --dry-run   # preview only: what would be created/overwritten, changes nothing
procrafiler deleted-history         # list library files you deleted by hand (from the action log)
procrafiler deletion-mode [MODE]    # show, or set how a hand-deleted doc is recorded (tombstone|purge)
procrafiler language [CODE]         # show, or set your primary language (search works in it + English)
procrafiler reindex                 # build/refresh the persistent content index so search never re-reads files
procrafiler enrich-keywords         # one-time: add existing docs' keywords in English + your language (uses AI)
procrafiler reconcile-snapshot      # rebuild catalog_snapshot.json from the DB if they drifted
procrafiler library-trash <path>    # move a library file to Library_Trash_Manual (you delete manually)
procrafiler purge-mirror-trash      # delete old mirror backups from Mirror_Trash by TTL
```

Setup & configuration:

```bash
procrafiler setup                   # guided first run: choose where files live (Inbox/Library/optional Mirror), then who you are
procrafiler init-layout             # create the workspace/library/state folders (idempotent)
procrafiler features                # list feature flags (see "Feature Controls" below)
procrafiler feature-set <name> <on|off>   # toggle actions_log / catalog_snapshot / mirror_sync
procrafiler policy-effective        # show effective runtime policy (see "Runtime Policy" below)
```

Mutating commands (`process-*`, `rescan`, `scrub`, `backup`, `restore`, `library-trash`, `purge-mirror-trash`, `enrich-keywords`) take a runtime lock, so two runs never race on the same Inbox. That is the complete command surface (run `procrafiler --help` for the live list).

**`restore` never destroys a document.** It shows what it would change and **asks** before replacing anything that differs; each replaced document is moved to `Library_Trash_Manual` (recoverable), never overwritten in place. Use `--dry-run` to preview and `--yes` to skip the prompt in a script. A restore **merges** into your library: documents that exist only there are left untouched.

**Your Inbox, Library and Mirror must be separate folders — none inside another.** `setup` refuses an overlapping layout (a mirror inside the library would get swept up by the library scan and corrupt the catalog), and `doctor` fails if it finds one, so a hand-edited env file cannot slip one past.

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

- Version format: SemVer (`MAJOR.MINOR.PATCH`), tags `vX.Y.Z`.
- **The git tag is the version** — `procrafiler --version` is derived from the latest tag (setuptools-scm); there is no version number to edit by hand. Release process: [docs/release.md](docs/release.md).
- Changelog: [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog format).
- Detailed process: [docs/release.md](docs/release.md).

## Install, configure, run

Linux (Ubuntu-first). Prerequisites: `git` and `python3` (3.11+). Full guide: [docs/ubuntu-deploy.md](docs/ubuntu-deploy.md).

**1. Get the code and install:**

```bash
git clone https://github.com/Simon-LM/ProcraFiler.git
cd ProcraFiler
./scripts/install.sh --mode user      # installs `procrafiler` into ~/.local/bin
# system-wide instead: sudo ./scripts/install.sh --mode system   (binary in /usr/local/bin; add --prefix /usr for /usr/bin)
```

The installer creates an isolated virtualenv and, on first install, an env file seeded from `.env.example` (its location is printed at the end of the install).

**2. Run the guided setup** — choose where your files live, then tell the app who you are (one guided first run):

```bash
procrafiler setup
```

It runs three short steps: **(1) where your files live** — Inbox, Library and an optional **Mirror** (a backup copy, ideally on a **different disk**, e.g. Library on SSD + Mirror on HDD; decline it and none is created); **(2) which AI** — the **Mistral API** (default, asks for your key) or **all-local Ollama**, or skip and edit the env file yourself; **(3) who you are** — a short questionnaire that helps the AI file your documents. Press Enter to accept each default; re-run any time.

**3. AI details (optional).** `setup` already configured the AI. The default is the **Mistral API** — just make sure `MISTRAL_API_KEY` is set. To run fully **locally** (Ollama) or mix per task, see **[docs/ai-providers.md](docs/ai-providers.md)** — tested models and recommendations **by GPU VRAM**. Your env file:

- user install: `~/.config/procrafiler/procrafiler.env`
- system install: `/etc/procrafiler/procrafiler.env`

**4. Verify, then run:**

```bash
procrafiler --version    # confirms the install (tracks the release tag)
procrafiler doctor       # checks paths, env, AI config, catalog
# drop files into your Inbox, then:
procrafiler process-all
```

### Try it first, safely (the sandbox)

Before pointing ProcraFiler at your real files, run the whole pipeline end to end in a throwaway **sandbox** that **never touches your Downloads or home** — `run.sh` forces every path inside `sandbox/workspace/`. From the clone:

```bash
./sandbox/run.sh e2e     # create layout + seed synthetic samples + process-all + show the result
```

The first run **creates its own virtualenv automatically** — nothing to set up. Without an AI key it runs in safe fallback mode (files go to manual review); add a key to the repo `.env` to see real reading + classification. Any command works against the sandbox too (`./sandbox/run.sh search facture`, `./sandbox/run.sh status`). Once you're happy, configure the real paths above and run for real. See [sandbox/README.md](sandbox/README.md).

## Running the tests

```bash
make test          # routine suite: offline, deterministic, no API calls
make test-ollama   # opt-in: real local-model integration (needs Ollama running)
```

The routine suite is **offline by design** — it never calls a real AI provider
(no spend, deterministic, free). Two layers ensure this: `tests/__init__.py`
points `PROCRAFILER_ENV_FILE` at an empty file so the suite never loads your real
`.env`, and the app itself refuses to auto-load the cwd `./.env` when it detects a
test runner — so even a bare `python -m unittest discover -s tests` stays offline.
`make test` (which runs `-t . -s tests`) is the canonical command.

When a test fails, or to learn how the suite is run and kept offline, see
**[docs/testing.md](docs/testing.md)**.

## Update

From the Git clone on the machine:

```bash
./scripts/update.sh --mode user
# or: sudo ./scripts/update.sh --mode system
```

The updater fetches the tags and moves the clone to the **latest release tag** (`vX.Y.Z`) — never an in-progress branch HEAD — then reinstalls in the venv. It prints `old → new` version and refuses to run if the clone has local changes. **Your library, catalog, settings and env file are never touched**, and the catalog migrates in place — so an update never forces you to reorganize anything. `procrafiler --version` always matches the installed release.

## Uninstall

```bash
./scripts/uninstall.sh --mode user
# or: sudo ./scripts/uninstall.sh --mode system
```

This removes the app (launcher + venv + code) and **keeps everything else** — your library, the catalog/state, and your config (incl. the env file with your keys) — printing exactly what is kept and where. **Your organized files are never deleted.**

To also remove the config and regenerable state (env file, settings, policy, catalog, logs, search index) — but **never** your library or your context file — add `--purge` (it lists the files and asks for confirmation; `--yes` skips the prompt):

```bash
./scripts/uninstall.sh --mode user --purge
```

## Project Status

The IA-first core is implemented end to end:

- **Reading** — every file is read for its content: text files and readable PDFs locally (via `pypdf`), scanned PDFs via OCR (Mistral by default, or a local Ollama model), images via a vision model (Mistral or local). Which provider/model per task is your choice — see [docs/ai-providers.md](docs/ai-providers.md).
- **Naming** — the new filename is derived from that content.
- **Classification** — the destination category is decided by AI from that content, never a guessed category. When the AI is unsure but has candidates, the file enters the **decisions queue** for you to resolve with `procrafiler review`; truly unreadable or optionless files go to plain manual review (`Manual_Review`).
- **Safety** — the app never deletes your files: duplicates and removals are *moved* to dedicated trash folders for you to empty manually; the only deletion is the explicit `purge-mirror-trash` command, scoped to old mirror backups in `Mirror_Trash`.

Every AI task is configured per the env variables above (provider/model never hardcoded); with no chain configured, files simply route to manual review.

Not yet implemented: the optional `SUPERVISOR` control pass, and `VIDEO` / audio analysis (planned for a later phase). API request/response contracts for OCR and vision should be confirmed with a live key.
