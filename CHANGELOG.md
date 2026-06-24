<!-- @format -->
<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Added

- **AI provider selection, made simple.** A new **[docs/ai-providers.md](docs/ai-providers.md)** explains the two providers (Mistral API vs local Ollama), the per-task chains, and gives **ready-to-paste profiles** (all-API / all-local / mixed) with **tested models** and **recommendations by GPU VRAM**. `.env.example` is rewritten to match and points to the guide. `procrafiler setup` now has an **AI step**: pick *Mistral API* (default — it also stores your `MISTRAL_API_KEY`), *all-local Ollama* (writes a tested preset: `gemma4:12b` / `minicpm-v` / `qwen2.5vl:7b`), or *configure it later*. `mistral-ocr-latest` keeps you on the newest Mistral OCR.
- **Guided first-run `procrafiler setup`.** Instead of hand-editing the env file, a single guided run asks where your **Inbox**, **Library** and an optional **Mirror** should live (defaults proposed, accept with Enter or type your own), writes those paths to the env file (keeping your AI key + chains), creates **only** the folders you chose, then flows into the "who you are" context questionnaire. The **mirror is optional** — decline it and no mirror folder is created and `mirror_sync` is turned off (the pipeline, `doctor` and `init-layout` all honour that). When kept, `setup` advises putting the mirror on a **different disk** than the library (e.g. SSD + HDD) and **warns** if you pick the same disk — a mirror there wouldn't survive that disk failing. `install.sh` now points to `procrafiler setup` as the next step.

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
