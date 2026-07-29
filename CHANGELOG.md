<!-- @format -->
<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Changed

- **The vision model is now told the file's name and the folder you dropped it in.** It used to read your photos completely blind, and some photos simply cannot be read on their own: a close-up of green fibres is a lawn or a soaked carpet depending only on what surrounds it, and nothing *in* the image settles it. Measured on the same image, the model unaided answers "an abstract pattern" or hedges "grass or fabric"; told it came from `Degats-eaux-salon/tapis-detrempe.jpg` it reads a carpet, told it came from `Jardin-printemps/pelouse-tondue.jpg` it reads a lawn. The obvious risk is the model just agreeing with the name, which would corrupt the one signal meant to be independent of it — so the prompt grants these names exactly one power, **breaking a tie on an ambiguous image**, and explicitly denies them the power to add anything: they are declared fallible, possibly meaningless (`IMG_2024`, `scan_001`) or plainly wrong, and the model is told to contradict them when the image does. Verified against the real API: a photo of a garden deliberately misnamed `facture-EDF-mars-2026.jpg`, in a folder called `Factures-2026`, is still read as a garden and is *not* flagged as a document. Confirmed again on **15 real photographs** of a water-damage claim, where the effect is larger than on generated images: a photo read blind as "the inside of an open washing machine" becomes "under a kitchen unit, electrical cables, water-damage marks", and a blind reading that **invented text** stopped doing so. A photo in the same folder but unrelated to the claim read identically with and without the hint — the folder's subject is not projected onto it. Only image reading is affected — OCR transcribes what is on the page and needs no hint.

- **A photographed document is now transcribed, not merely described.** A photo is sent to the vision model because it is a `.jpg` — even when it *is* a document, so an invoice snapped with a phone came back as "an administrative document with a logo" instead of its amount, reference and date. Worse, that weak text is what gets cached in the search index, so the loss was permanent rather than one bad filing decision. The vision model is now asked whether the image is primarily a written document; if it is, the file is re-read with the **OCR** model, which is built for exactly that. **Both** texts are kept — the transcription first and labelled reliable, the visual description after as context — so you get the figures *and* the surroundings. Costs a second AI call, but only for photos of documents: a photo of water damage triggers none. Such a file also counts as a reliable read from then on, which is now accurate, since its main content is a real transcription.

- **Dropping a folder now weighs on how its files are named.** Until now each file was named from its own content alone, blind to everything dropped with it — so a photo the vision model misread kept an absurd name in the middle of nine coherent neighbours. After every file of a dropped folder has been read, a new **naming pass** (`PROCRAFILER_AI_NAMING_*`, Mistral medium by default) re-judges their names **together**, before any filing. It is not a grouping mechanism: a folder may legitimately hold several unrelated subjects and then each file keeps its own identity — coherent groupings simply tend to emerge from the context. A file it truly cannot settle goes to the decisions queue instead of being named on a guess. Loose files at the Inbox root are unaffected, and with no naming chain configured nothing changes.

- **The original filename now counts for more when the content is less reliable.** ProcraFiler still never lets a filename *decide* — the content does — but the name was being weighed the same whether the text came from a PDF's own text layer (literal, trustworthy) or from an AI describing a blurry photo (a guess that can be wrong or invented). That is backwards. The analysis now knows **how** the file was read: for a mechanical read nothing changes, but for a **photo read by a vision model** the filename, the folder you dropped it in and the names of the files you dropped with it become **corroborating evidence** (OCR is not affected — transcribing text off a page is reliable; it is image *description* that is weak) — specific evidence outweighs a vague visual description, and a clear contradiction sends the file to the decisions queue instead of being guessed. So a photographed document in a well-named folder is no longer at the mercy of the vision model alone.
- **Files dropped together now inform each other.** Only the folder *name* used to be passed to the AI; the names of the **other files in the same drop** were discarded. They are a free and often decisive clue — a photo among `facture_plombier.pdf` and `constat_amiable.pdf` is about that affair — so they are now part of the context for each file of the set (capped, so a huge folder cannot inflate the prompt or the cost). Loose files at the Inbox root stay singletons and get no invented context. Note this could not be left to the later grouping pass, which works on records already produced — by then a misreading has already happened.

### Fixed

- **The env file you name is now the one that is used — or none at all.** `PROCRAFILER_ENV_FILE` could be silently ignored: the app tested it with `is_file()`, which rejects `/dev/null`, so the documented way to force an offline run instead loaded the developer's `./.env` — real API key and provider chains included. A typo in the path did the same. The named file is now **authoritative**: it is the only one tried, the search never falls through to another source, and readability is decided by actually reading it (so `/dev/null` legitimately loads nothing). When the named file cannot be read, nothing is loaded and **`doctor` fails** instead of letting the run use built-in defaults without a word.

- **A failed filing can no longer destroy the document (regression fix).** The atomic-placement work above staged each document into a hidden temporary file before renaming it into place — but it *moved* the document into that staging file, which removes the original. If the final rename then failed, the document existed **only** in the staging file, and the next run's cleanup deleted it. Placement now uses a single atomic rename when the Inbox and library share a filesystem, and otherwise **copies** to the staging file and removes the original only once the document is safely in place. Either way the original survives until the document really lands, so nothing can be lost and the next run recovers it.
- **A filename the AI makes too long no longer blocks filing.** The descriptive name comes from the AI, and a model that answers with a whole sentence instead of a title produced a filename the filesystem refuses (over 255 bytes) — which failed the filing of a perfectly good document. Names are now capped (cutting at a word boundary), so any AI answer yields a valid filename.
- **`restore` can no longer silently overwrite a newer document.** It is a *recovery* command, but pointing it at a stale mirror used to copy straight over your library with no confirmation, no preview, and no copy kept — so "let me just check that restore works" could roll your library back without a word. (The tell: the catalog DB *was* backed up before being replaced; your documents were not.) Now: **`restore --dry-run`** shows exactly what would be created, overwritten or left alone and changes nothing; a restore that would replace differing documents **asks first** (`--yes` to skip, for scripts); and each replaced document is **moved to `Library_Trash_Manual`**, recoverable, instead of destroyed — the same never-delete rule the rest of the app follows. Documents that exist only in your library are reported as untouched, so it is clear that a restore **merges** rather than replaces. Applies to both `--from <mirror>` and `--from-archive`.
- **Overlapping folder choices are now refused, not just discouraged.** `setup` accepted any paths that were merely *different*, so putting the Mirror **inside** the Library (or the Library inside the Inbox) went through silently — after which the library scan swallowed the mirror: mirror copies got renamed, phantom duplicate entries entered the catalog, a `Mirror/Mirror/` level appeared, and every unrecognised mirror file cost a real AI call. `setup` now **refuses** an overlapping layout, explains which two locations clash, and asks again; `doctor` **FAILs** on an overlapping layout that already exists (e.g. hand-edited into the env file). The check covers the folders you never type — the library trash and the app state — and compares resolved paths.
- **`doctor` now checks the things that actually matter before you trust the app**, not just that folders exist and are writable: documents stranded in the `Queue` (FAIL), overlapping locations (FAIL), and a **mirror sitting on the same disk as the library** (WARN — `setup` said this once at creation and never again).

- **An interrupted run no longer loses your files.** A file leaves the Inbox for the internal `Queue` **before** the (slow) AI read, and nothing ever looked in the `Queue` again — so any hard stop (Ctrl-C, `SIGKILL`, OOM, power loss, a closed SSH session) left those documents **invisible**: gone from the Inbox, absent from the library and the catalog, while `process-all` reported a clean run and `doctor` reported zero failures. Every `process-once` / `process-all` now **recovers the Queue first**, returning each stranded file to the **exact Inbox subfolder it was dropped in** (so files dropped together as a set are not scattered), and reports how many it recovered. Recovery is idempotent — a crash *during* recovery just leaves the rest for the next run. This matters most with local models, where a file can take minutes and interrupting a batch is ordinary.
- **`doctor` now FAILs while documents sit in the `Queue`** (naming them, exit code 1) instead of reporting a clean bill of health. `doctor` is the command you run to decide whether to trust the app — it must not stay silent about invisible files.
- **A document can no longer be left truncated in the library or the mirror.** `shutil.move` degrades to copy-then-delete across filesystems, and the recommended layout puts the library and mirror on **different disks** — so an interrupted placement could leave a **half-written file at a real document path**, which the next `rescan` would then ingest as a genuine new document (reading garbage, cataloguing and mirroring it), or which a later `restore` would trust as the good copy. Library placement and mirror sync now stage into a hidden temporary file and **atomically rename** it, so a real path only ever holds a complete, hash-verified document. Interrupted leftovers are swept automatically.

### Security

- **A development build can no longer write into your real library.** ProcraFiler's default paths are rooted in your home directory, and about thirty commands build the layout from them — so anyone working on the code, from a checkout, was one forgotten environment variable away from pointing a half-finished build at their own documents. That is exactly what happened here on 2026-07-28: a development run created a full layout (inbox, library taxonomy, mirror, state) in the developer's real home. Nothing was lost, because it was empty and no document was ever processed, but nothing in the code prevented it either. Now, when the package is imported from a source checkout, it **refuses** to write to (1) the layout the installer recorded — read from your own configuration, so it protects a library you moved out of `$HOME`; (2) any layout that already holds documents and is not marked as a sandbox; (3) the built-in default paths. Each refusal names the four directories it declined to touch and how to run the sandbox instead; `PROCRAFILER_ALLOW_REAL_DATA=1` overrides all three on purpose. **If you installed ProcraFiler normally — the installer, or `pip install` — none of this applies to you and nothing changes**: the check is on whether the package was imported from a checkout, never on your paths. A development sandbox marks itself on creation, so it keeps working once it fills up with test documents.

- **The test suite is now proven to write nothing into your home**, rather than assumed to. `make test-isolation` re-runs the whole suite with `HOME` redirected to an empty directory and fails if anything appears there — anything landing in that fake home would have landed in a real one. It runs in CI on every push. The suite was already clean; this is what keeps it that way.

### Added

- **`make test-mistral` — opt-in tests against the real Mistral API** (skipped by default; they cost money). Everything else in the suite mocks the AI, so it can only prove a prompt was built — never that the model *judges well*. These measure the one thing that matters for the naming pass: a photo whose vision reading went wrong (a soaked carpet read as a lawn, a stained ceiling read as the sky, crumpled bodywork read as an abstract sculpture) is recontextualised into the set it belongs to, while a genuinely unrelated photo is left alone. Verified across two unrelated domains, so the judgement is generalist and not water-damage-specific.
- **A durability test suite that tries to break the app on purpose** (`tests/test_durability_audit.py`, `tests/test_durability_processes.py`). It kills a real run with `SIGKILL` mid-processing and checks every document is still there byte for byte; runs two real processes against one Inbox at once; injects disk-full, permission and I/O errors at each write; feeds in filenames containing newlines, tabs, quotes, unicode, right-to-left overrides and shell metacharacters; and verifies `scrub` heals a corrupted copy from the good one in both directions — and refuses when both are bad. No new defect was found: the app already handled all of it. **614 tests**, still offline and deterministic.

- **`docs/pre-prod-hardening.md`** — the gated checklist from the pre-production audit (what blocks the first real-files run, what blocks recommending the tool, and the test-audit gate), with reproduced evidence for each item.
- **`tests/test_restore_safety.py` and `tests/test_layout_conflicts.py`** — 23 tests covering the restore preview/prompt/trash-rescue path and the overlapping-layout guard (including one asserting the shipped default layout passes its own check).
- **`tests/test_filename_hint_weighting.py`** — 16 tests asserting the hint framing flips with the read method and that the set's filenames reach the per-file prompt, end to end through the real pipeline (offline: they check the prompt that gets built, never a live call).
- **`tests/test_crash_recovery.py`** — durability tests for interruption and corruption, built around a **conservation invariant**: for N dropped files, with an interrupt injected at *every* pipeline step in turn, every file stays accounted for exactly once — and every original content hash is still on disk (a count check cannot catch a truncated file). Offline and deterministic like the rest of the suite.

## [0.8.0] - 2026-06-26 — Stabilisation: local-AI tuning, durability fixes & a hardened test pass

The stabilisation milestone before real-world testing (the last gate before v1.0.0). It tunes local AI for slower machines, fixes two data-durability edge cases, and lands a broad **offline test pass** — install / update scripts, local-AI end-to-end, mirror consistency, and the durability commands (`scrub` / `verify-catalog` / `backup` / `restore`) with their edge cases — so the path to 1.0 rests on a tested foundation.

### Changed

- **Local AI: better default model + provider-aware timeouts.** Local **analysis** now defaults to **`qwen3.5:9b`** — it returns clean JSON and at 6.6 GB fits a 12 GB GPU (no CPU spill → faster *and* cooler than `gemma4:12b`, which stays the default for the harder `organize` task). And the per-call **timeout is now provider-aware, with two separate knobs**: `PROCRAFILER_AI_TIMEOUT` for the **Mistral API** (moderate, 60 s) and `PROCRAFILER_AI_LOCAL_TIMEOUT` for **local Ollama** (generous, 15 min — applied automatically). So a merely-slow local call (weak machine, large file) is no longer killed and dropped to manual review, and you can tune API and local independently. A per-task `PROCRAFILER_AI_<TASK>_TIMEOUT` overrides either. (`qwen3.5:9b`'s earlier "empty" results were just the old 60 s default cutting off its ~87 s generation.)
- **Local AI calls now stream — the local timeout is a *no-progress* timeout.** Ollama text calls (analysis/organize/grouping) consume the response token by token, so `PROCRAFILER_AI_LOCAL_TIMEOUT` is an **idle (no-progress) timeout, not a total deadline**: as long as the model keeps producing, it is **never** killed — however slow the machine or large the file — and only a truly *stalled* call (no output for that long) is aborted. No arbitrary total cap to guess.

### Fixed

- **`restore` fails cleanly on an unreadable backup archive.** A backup so corrupted it is no longer a valid `tar.gz` (e.g. a damaged encrypted archive whose header was lost) now reports a clear "unreadable or corrupted" message and exits non-zero, instead of dumping a `tarfile.ReadError` traceback. (Realistic bit rot in an encrypted archive — body tampered, header intact — was already handled.)
- **The mirror now follows a hand move/rename.** When you reorganise the library by hand and `rescan` repoints the catalog, the document's mirror copy **and its hidden text sidecar now move with it**, instead of being orphaned at the old path with nothing at the new one. The mirror stays a faithful path-for-path replica, so `scrub` and heal find every document where they expect it (no stale orphans, no false "missing" reports). Backed by new offline mirror-consistency tests.

## [0.7.0] - 2026-06-25 — Encrypted backups

Completes the data-durability work (v0.6.0) with encrypted cold backups, and is the last feature step before the 1.0 stabilisation pass (more tests, then real-world testing).

### Added

- **Encrypted backups — `procrafiler backup --to <dir> --encrypt`.** Protects the cold backup bundle with a passphrase (**AES-256-GCM**, key derived via scrypt), for cloud/offsite storage. The output is `…tar.gz.enc`; `restore --from-archive` detects an encrypted backup and asks for the passphrase (or reads `PROCRAFILER_BACKUP_PASSPHRASE`). The passphrase is prompted twice (confirmed) at backup time and never stored — keep it safe, as it cannot be recovered. Adds the `cryptography` dependency.

## [0.6.0] - 2026-06-25 — Data durability: detect, heal, recover

ProcraFiler now protects the archive itself. It detects **silent corruption** (bit rot / tampering), **repairs** it from a good copy, can **rebuild** a damaged catalog, **restart** from a mirror after a disk loss, and write **immutable offline backups** — all on the existing local mirror, pure Python, no new dependencies. The full design (and the Phase 2–4 roadmap: LAN/multi-replica, cloud via rclone, SMART) is in [docs/durability.md](docs/durability.md).

### Added

- **`procrafiler scrub` — integrity check & self-healing.** Re-hashes stored documents and compares them to the catalog `sha256`, on the **library** and the **mirror**, so silent corruption (bit rot) or tampering is detected. Incremental (`--limit N`, least-recently-verified first) or full; `--no-mirror` to skip the mirror; exits non-zero on any problem. A new `last_verified_utc` catalog column records when each document was last checked. With **`--repair`** it **heals**: a bad copy is restored from a verified-good one (library ↔ mirror), atomically and re-verified — never from a source that doesn't itself match the catalog, and never when all copies are bad (reported as unrecoverable). Repairs are written to the action log.
- **`procrafiler verify-catalog` — catalog durability.** Checks the SQLite catalog with `PRAGMA integrity_check`; with **`--rebuild`** it reconstructs the DB from the corruption-resistant `catalog_snapshot.json` when the DB is corrupt or lost (the old DB is moved aside, never deleted) — your search/dedup/provenance survive a damaged database. Reports cleanly when there is no usable snapshot (restore from a mirror instead).
- **`procrafiler restore --from <mirror>` — disaster recovery.** Rebuilds the library **and** catalog from a self-contained mirror after a loss (e.g. the primary partition died), re-rooting document paths to the configured library location. The mirror is now a **self-contained unit**: `scrub` refreshes a catalog snapshot inside the mirror's `.procrafiler/` folder, so a mirror carries both your files and the catalog needed to restart from it. (Any existing catalog is moved aside, never overwritten.)
- **`procrafiler backup` — immutable offline backups.** `backup --to <dir>` writes a **consistent, self-contained, dated** archive of the library + catalog (`procrafiler-backup-<date>.tar.gz` + a `.sha256`), taken under the lock with a fresh snapshot so files and catalog match. It's immutable — each run is a new dated archive to keep on **offline / air-gapped** media; `restore --from-archive <file>` rebuilds from it (re-rooting to your library). `status` shows the last-backup date and **reminds** you when one is overdue. (Bundle encryption is the next step; for cloud, use `rclone crypt`.)

## [0.5.0] - 2026-06-24 — Guided first-run setup & AI selection

A friendly first run and a clear way to choose the AI. `procrafiler setup` now walks you through **where your files live** (Inbox, Library, optional Mirror — advised on a separate disk), **which AI** reads them (Mistral API by default, or all-local Ollama from a tested preset), and **who you are** — writing your env file for you, with no hand-editing. The whole first-run interface is now in **English**.

### Added

- **Guided first-run `procrafiler setup`.** Instead of hand-editing the env file, a single guided run asks where your **Inbox**, **Library** and an optional **Mirror** should live (defaults proposed, accept with Enter or type your own), writes those paths to the env file (keeping your AI key + chains), creates **only** the folders you chose, then flows into the "who you are" context questionnaire. The **mirror is optional** — decline it and no mirror folder is created and `mirror_sync` is turned off (the pipeline, `doctor` and `init-layout` all honour that). When kept, `setup` advises putting the mirror on a **different disk** than the library (e.g. SSD + HDD) and **warns** if you pick the same disk — a mirror there wouldn't survive that disk failing. `install.sh` now points to `procrafiler setup` as the next step.
- **AI provider selection, made simple.** A new **[docs/ai-providers.md](docs/ai-providers.md)** explains the two providers (Mistral API vs local Ollama), the per-task chains, and gives **ready-to-paste profiles** (all-API / all-local / mixed) with **tested models** and **recommendations by GPU VRAM**. `.env.example` is rewritten to match and points to the guide. `procrafiler setup` now has an **AI step**: pick *Mistral API* (default — it also stores your `MISTRAL_API_KEY`), *all-local Ollama* (writes a tested preset: `gemma4:12b` / `minicpm-v` / `qwen2.5vl:7b`), or *configure it later*. `mistral-ocr-latest` keeps you on the newest Mistral OCR.

### Changed

- **The guided first run (`setup` + `setup-context`) is now in English** for the whole interface, matching the install commands and the open-source/general-public audience. (The written context file the AI reads was already English; only the interactive prompts changed.) A selectable interface language — French first, then others — is planned.

## [0.4.0] - 2026-06-23 — Solid install, update & uninstall

Installing, updating and uninstalling are now solid, and **an update never forces you to reorganize your folders**: your library, catalog, settings and keys are preserved across versions, the catalog migrates in place, and the version always tracks the release tag. This is the groundwork for a 1.0 — what still remains for that milestone is an **interactive install** (choose where your Inbox / Library / Mirror live) and a **clearer way to pick the AI providers** (local vs API, per task). (Everything from the earlier 0.x line is included: AI-first naming + classification, set-aware organize, the decisions queue + `review`, hand-reorganization `rescan`, offline + AI-assisted multilingual `search`, and a tombstone/purge deletion model.)

### Added

- **The dev sandbox ships with the repo.** A ready-made, fully isolated end-to-end harness (`./sandbox/run.sh e2e`) on synthetic sample files, for trying ProcraFiler before pointing it at your real files. `run.sh` forces every path inside `sandbox/workspace/` itself (so a run can never touch your real files, no `.env` path setup) and **bootstraps its own virtualenv on first run** — a fresh clone needs only `git` + `python3` and one command. Only the generated workspace is gitignored; the README and `docs/testing.md` point to it for manual end-to-end testing.

### Changed

- **Versioned by the git tag.** `procrafiler --version` is derived from the latest tag (setuptools-scm) — one source of truth, no hardcoded number to drift (it used to report `0.2.0` while releases were at `v0.3.3`). (#79)
- **`update` tracks the latest release tag.** `update.sh` fetches the tags and checks out the newest `vX.Y.Z` (never a branch HEAD), reinstalls, and prints `old → new`; it refuses a clone with local changes and never touches your library, catalog, settings or env. (#79)
- **`uninstall` is clear and safe.** It prints exactly what is kept (library, state, config) and guarantees your organized files are never deleted; a new opt-in **`--purge`** removes the config + regenerable state — but never the library, mirror, trashes or your context file (it lists the files and asks for confirmation). (#81)

### Fixed

- **A fresh install writes a correct AI config.** `install.sh` now seeds the env file from the canonical `.env.example` (one source of truth) instead of a hardcoded template that had drifted — it listed dead AI tasks and was missing the ones actually used (`ANALYSIS`, `ORGANIZE`). The file is created `0600`; an existing env file is left untouched. (#80)

### Tests

- The install/update/uninstall guarantees are regression-tested: catalog **schema migration** on an old DB, **settings forward-compatibility**, and the real `uninstall.sh` proving the **library survives** both `uninstall` and `--purge`. (#82)

## [0.3.3] - 2026-06-23 — AI-assisted search

The offline `search` stays instant and free; a new opt-in `search-ai` brings in an AI to broaden a query with synonyms and translations for the cases where exact search is too narrow.

### Added

- **`search-ai` — deeper search, powered by AI.** `search-ai <word or phrase>` asks a small AI to broaden your query with **synonyms and translations** (English + your language), then runs the offline search over all of them at once (OR, BM25-ranked). So `search-ai acoustique` surfaces documents indexed under `audio`, `son`, `sound`, `sonore`… that a plain `search acoustique` would miss. Common function words are dropped from the broadened query so an ambiguous short word (e.g. the French possessive `son`) doesn't drown the real hits. It prints the terms it added (transparency), and **falls back to the plain offline `search`** when no AI chain is configured. The default `search` is unchanged — offline, instant, no AI. New `expand_query` (`ai_analysis`) + `search_catalog_any` (`search`); CLI `search-ai`. `tests/test_search_ai.py` +7. (#78)

## [0.3.2] - 2026-06-23 — Multilingual & forgiving search

`search` now forgives typos and crosses languages: a misspelled word still finds its document (offline, no AI), and a document is findable by its category in your language as well as English. You set your primary language once.

### Added

- **Typo-tolerant `search`, offline** — when an exact search finds nothing, each word is widened to its closest indexed terms (edit-distance over the index vocabulary, no AI), so `pasisons` finds `passions` (and a plural/typo still lands). A correctly-spelled query stays exact — the fuzzy fallback fires only on zero results. (#77)
- **Category search in your language and English** — a document is now findable by its **category** (e.g. `Hobbies` is found by `hobbies` AND by `loisirs`/`passion`), via a curated translation map of the small fixed base-folder tree. Offline and applies to every existing document. (French provided; another language is just one more entry in the map.) (#77)
- **Primary language — auto-detected, zero configuration** — the app infers the user's language from the languages of their own catalogued documents (the AI records each document's language in its fiche), so a French user's library just works in French. An explicit choice in `setup-context` or `procrafiler language <code>` always overrides it; shown in `status`. This drives the cross-language category search and the keyword enrichment. (#77)
- **Bilingual keywords for newly filed documents** — the analysis call now produces keywords in English AND your language (and the summary/name in your language) instead of hardcoded French, so new documents are searchable either way. (#77)
- **`enrich-keywords` — back-fill the same bilingual keywords onto your EXISTING documents** (one AI call each, text-only — no file re-reading). Normally never needed — `run` and `rescan` already do this for every document they read; it is a safety net for documents filed before this feature. A document already enriched is skipped (safe, cheap to re-run); `--force` re-processes everything (e.g. to refresh relevance with a better model later). A no-op when your language is English or no AI chain is configured. (#77)

## [0.3.1] - 2026-06-22 — Search index

Completes the Search work from 0.3.0: search no longer re-reads your files — the extracted body text is cached in a persistent, content-hash-keyed index, warmed as you search and (re)built by the new `reindex` command.

### Added

- **Persistent content index — `search` no longer re-reads files (Search Slice 4).** Deep search (Slice 3) read each document's body on disk at query time, re-extracting PDFs on every search. The extracted body text is now cached in a dedicated `search_index.db`, **keyed by content hash** (so a moved/renamed file never invalidates it and duplicates share one entry) and kept out of the main catalog. The index **warms itself** as you search (each body read once, then served from cache), and the new **`reindex`** command pre-builds or refreshes it in one pass (the backfill: adds missing bodies, prunes content no longer present). A deleted document's body is dropped from the index too, so a purged/tombstoned document's text doesn't linger. `status` shows `search_index_file`. New `procrafiler.search_index` (`BodyTextIndex`) + `reindex_content`; CLI `reindex`. (#76)

## [0.3.0] - 2026-06-22 — Search

Find any filed document **offline** — by what it _is_ (its fiche) and what it _says_ (its body text: OCR/vision, plain text, PDF). Plus a privacy-minded deletion model (tombstone / purge) and a test suite that is now enforced in CI and can never touch the live API.

### Added

- **`search` — find documents by their fiche, offline (Slice 1)** — `procrafiler search <terms>` queries the per-document fiche (name, keywords, entities, summary) via SQLite **FTS5**: BM25-ranked, **accent-insensitive** (`impot` finds `impôt`), no re-reading, no AI, no network. A temporary index is built from the catalog at query time, so it's always consistent and there's nothing to migrate. (#67)
- **Deep content search — `search` also looks inside the document body (Slice 3)** — a word that appears only _inside_ a document now surfaces it. Body text is read with no AI and no network: from plain-text files and text-layer PDFs directly, and from the Slice-2 sidecar for scans/images. A name/keyword match still outranks a body-only match. (#75)
- **Hidden text sidecars for OCR/vision documents (Slice 2)** — the costly AI-extracted text (OCR for scanned PDFs, vision for images) is cached **once** in a hidden `.<filename>.txt` next to the file, so search reads it without ever re-OCR'ing. rescan moves it with its document and backs it up to the mirror; on deletion each copy goes to its own library's trash. (#69)
- **`deletion-mode` — choose what a hand-deleted document leaves behind** — `tombstone` (default: keep id + content hash + date, so re-adding it is recognised, never re-trashed as a duplicate) or the opt-in `purge` (keep nothing; the deletion survives only in the action log). For the rare case where even the fingerprint must not remain. (#74)
- **Continuous integration + a testing guide** — the repo's first GitHub Actions workflow runs the offline suite (`make test`) on every push and pull request; `docs/testing.md` explains how the suite stays offline and what to check when a test fails. (#73)

### Fixed

- **Deleting a document then re-adding it is no longer mistaken for a duplicate** — a hand-deleted file's catalog row used to be kept whole and matched by hash, so re-dropping it was trashed as a duplicate. De-duplication now ignores deleted rows (you're told you had deleted it before, and it's re-filed), and the row is reduced to a tombstone — id + hash + date only — so nothing of its content lingers. The mirror copy and both hidden sidecars are quarantined to their own trash, and the action log keeps a trace. (#71)
- **Tests can no longer reach the live Mistral API by accident** — run any way other than the canonical `make test`, the offline guard was skipped and CLI-driven tests loaded the real `./.env`, so "offline" unit tests intermittently hit the real API (and looked flaky). The app now never auto-loads `./.env` when it detects a test runner; the real application is unaffected. (#72)
- **A file you resolve via `review` now also gets its hidden text sidecar** — it was written _after_ a parked file returned early, so review-resolved documents were invisible to deep search; it's now written before parking and moved out with the document on resolution. (#70)
- **rescan syncs the catalogued name to your filename** — renaming a filed document by hand updated its path but left the fiche `name` stale, so `search` showed the old name; rescan now follows the on-disk stem (no AI, no re-reading), and its summary gains `names synced`. (#68)

## [0.2.0] - 2026-06-19 — Rescan

When you reorganize the library by hand, the catalog now follows — with no AI; and "preserve zones" (git repos, Archive folders) are indexed for search without being touched.

### Added

- **`rescan` Phase 1** — follow hand moves/renames/deletes into the catalog with **no AI**: each file is tracked by its content fingerprint (sha256), so a moved/renamed file (or every file in a renamed folder) just has its catalog path updated; deletions are kept as `DELETED` rows and listed by the new `deleted-history` command; deliberate duplicates are catalogued, never acted on. Path-first, so an unmoved file is never hashed. (#58)
- **`rescan` Phase 2** — a brand-new file you drop in the library is read in full (its fiche enters the catalog, for search), gets the timestamp prefix (your stem kept), and a recurring kind is dated into its `<Entity>/<Year>/` subfolder — anchored at the folder you chose, never re-classified. (#59)
- **`Personal/Archive` and `Work/Archive`** — your keep-as-is zones: visible so you can drop backups/snapshots/old folders in them, but **excluded from the categories the AI may choose** (archiving is your decision). (#65)
- **git repositories are indexed for search** — a dropped repo's readable working-tree documents enter the catalog, while the repo is left untouched and `.git` internals are ignored. (#64)

### Changed

- **The horodatage is the app's** — any moved / renamed / duplicate library file lacking the `YYYY-MM-DD_HH-MM-SS__` prefix now gets one, built from its catalogued date, keeping your stem (a file that already has a valid prefix is never re-dated). (#63)
- **Images are classified by INTENT, not form** — a made-for-audience visual (a generated graphic, meme, infographic, comparison, avatar, social screenshot) is `Personal/Social-media`, not `Photo`; and when Personal-vs-Work can't be told, the file goes to the decisions queue instead of being guessed. (#66)
- **Non-decodable image formats** (`.xcf`, `.psd`, camera RAW, `.svg`) are no longer sent to the vision model (which rejects them); a large batch prints a heads-up before the heavy AI work. (#61)

### Fixed

- **rescan never descends into hidden directories or VCS repositories** — a dropped folder containing a `.git` had its repo internals and working tree timestamped/catalogued (corrupting the repo). A dropped repo is now left untouched as a unit. (#60)
- **rename-in-place + duplicate edge case** — rescan no longer treats the second copy of the same content as a brand-new file (which produced a malformed doubled prefix); it's a duplicate, and an existing full prefix is never re-applied. (#62)

## [0.1.0] - 2026-06-17 — Run

First tagged version: drop your files in one folder, an AI reads each one and names + files it by content. The usable base.

### Added

- **AI-first reading** — every file is read before anything else: local text and PDF extraction, OCR for scanned PDFs and vision for images (Mistral or local Ollama, chosen per task by env chains). The extension is only a technical dispatch signal, never a destination.
- **Content-driven naming** — the filename is derived from the document's content: a consistent structure per kind (CV, bill, statement, certificate…), the salient entity first, a `YYYY-MM-DD_HH-MM-SS__` timestamp prefix from the document's own date (EXIF for photos), no redundant words or dates.
- **Classification into a Personal / Work subject tree** — English, organized by life context then subject, never by file format. The AI reuses or creates subfolders from the content but may never invent a new top-level category; recurring kinds are filed as a dated series `<Entity>/<Year>/`, the year owned by the code.
- **Two-phase set-aware organize** — a folder you drop in is catalogued, then placed as ONE coherent affair/series instead of scattering. A run is monotonic: it only ever files a document _deeper_, never de-organizes.
- **Searchable catalog** — a per-document fiche (summary, keywords, entities, dates, provenance) is read once and kept, so files are searchable and reorganizable without re-reading; a human-readable `catalog_snapshot.json` and a synchronized backup mirror accompany it.
- **Decisions queue + `review`** — when the AI is unsure but has plausible options, it parks the file and lets you choose, instead of guessing or silently dumping to manual review.
- **Guided `setup-context`** — a universal questionnaire builds the user-context file the AI uses to disambiguate (which subjects are hobbies vs the job, who you are, banks/providers current and past, homes…). It guides, it never constrains — the document's content still decides.
- **Configurable, neutral AI sampling** — `PROCRAFILER_AI_TEMPERATURE` / `_TOP_P` env vars instead of a hardcoded `temperature=0.0`.
- **Operations & safety** — runtime lock (no two runs race), `doctor` diagnostics, snapshot reconcile, `library-trash`, recursive Inbox, per-file batch resilience, and automatic healing of accidental double-nesting.

### Changed

- **The Personal/Work decision is anchored to your declared context** — a document about a stated hobby stays Personal even when the gear/skill/venue is professional-grade; only your stated job/business leans Work.
- **Anti-magnet classification** — an existing subject folder is never a catch-all: a file goes there only when its content is clearly about that subject. A declared work-name routes to Work consistently; the genuinely miscellaneous goes to `<base>/Misc`. Base-tree folder names are English throughout (`Social-media`, `Misc`).

### Fixed

- **One slow/failed file no longer crashes the batch** — a network timeout becomes a retryable error, and any per-file exception is logged and skipped so the run finishes.
- **Empty Inbox subfolders are pruned** after their files are processed (with strict symlink-escape guards).

### Security

- Runtime env files are gitignored and chmod-protected (`0600`/`0640`); private keys and desktop artifacts are ignored. The updater no longer `source`s its metadata file as shell (no arbitrary-command path), parsing only known keys.
