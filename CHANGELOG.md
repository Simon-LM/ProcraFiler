<!-- @format -->
<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Added

- **Deep content search — `search` now looks inside your documents (Search Slice 3).** `search` matched only the per-document **fiche** (name, keywords, entities, summary); it now also matches the document's **body text**, so a word that appears only *inside* a document surfaces it ("find by what it says"). Body text is read with **no AI and no network**: from the document itself for plain-text files and text-layer PDFs, and from the hidden `.txt` sidecar (Slice 2) for scans/images whose text could only be read by AI — so a scanned PDF's content is searchable without ever re-OCR'ing it. A scanned page with no sidecar is still found by its fiche. Ranking puts a name/keyword match above a body-only match (BM25). The body is read at query time and capped per document; a persistent content index (so big PDFs aren't re-read each query) is the next slice. `tests/test_search.py` +4.

- **Hidden text sidecars for OCR/vision documents (Search Slice 2).** When a document's text could only be obtained by AI — OCR for a scanned PDF, vision for an image — that extracted text is now kept **once** in a hidden sidecar next to the file (`.<filename>.txt`). Plain text files and readable PDFs get **no** sidecar (their text is free to re-extract). This preserves the costly text on disk (respecting "process once"), so the upcoming deep search (Slice 3) can read a scan's/photo's content without ever re-OCR/re-vision. The sidecar is hidden (rescan's walk ignores it, it's never treated as a document); **rescan moves it with its document** when you reorganize by hand. It is also **backed up to the mirror** alongside its document, so the costly OCR/vision text survives even if you lose the primary library. On deletion each copy goes to **its own library's trash** (primary sidecar → `Library_Trash`, mirror sidecar → `Mirror_Trash`; see the deletion entry below). Written by the run and by rescan's ingestion. New `_write_text_sidecar` / `_move_text_sidecar` / `_mirror_text_sidecar`. `tests/test_sidecars.py` +6, `tests/test_rescan.py` +1. 361 tests green.

- **`search` — find your documents by content, offline (Search Slice 1).** A `procrafiler search <terms>` command queries the per-document **fiche** already in the catalog (name, keywords, entities, summary) via SQLite **FTS5** — no re-reading, no AI, no network. Results are ranked by relevance (BM25; the name/keywords weigh more than the summary), **accent-insensitive** (`impot` finds `impôt`), and shown as an accessible list (name · category · date · matching snippet · path). A temporary index is built from the catalog at query time, so it's always consistent and there's nothing to migrate. Covers every filed document, including Archive/VCS preserve-zone docs indexed in place. (Deep full-text search over the documents' body — hidden `.txt` sidecars for OCR/vision text + a persistent index — is the next slice.) New `procrafiler.search`; CLI `search`. `tests/test_search.py` +6. 353 tests green.

- **CI + a testing reference doc.** A GitHub Actions workflow (`.github/workflows/tests.yml`) runs the offline suite (`make test`) on every push and PR (no secrets exposed → enforced offline), and a new [docs/testing.md](docs/testing.md) documents how the suite is run, the two layers that keep it offline, and a "when a test fails" checklist. The repo had no CI before.

- **`deletion-mode` — choose what a hand-deleted document leaves behind (`tombstone` | `purge`).** By default (`tombstone`) a document you delete by hand leaves a minimal marker — id + content hash + deletion date — so re-adding it later is recognised (and never re-trashed as a "duplicate"). The new opt-in **`purge`** mode keeps **nothing** of the document in the catalog (no id, no hash, no fiche): the deletion survives only in the action log, and a later re-deposit is filed as a brand-new document. For the rare case where even the content fingerprint must not remain (e.g. a journalist's source). Set it with `procrafiler deletion-mode purge` (or `tombstone`), show it with `procrafiler deletion-mode` / `procrafiler status`; the setting lives in the app's settings file next to the feature flags. Either way, on deletion the mirror copy and both hidden text sidecars are still quarantined to their trash and the action log keeps a trace (now tagged with the `deletion_mode`). New `get_deletion_mode` / `set_deletion_mode` and `purge_document`. `tests/test_deletion_mode.py` +5, `tests/test_rescan.py` +1.

### Fixed

- **Tests can no longer reach the live Mistral API by accident (offline-by-default, any invocation).** The suite is forced offline by `tests/__init__.py` — but that package init only runs with the canonical `make test` (`-t . -s tests`). Run any other way (a bare `python -m unittest discover -s tests`, a single module, a future `pytest`), the guard was skipped and CLI-driven tests loaded the developer's real `./.env` (live key + chains) — so "offline" unit tests intermittently hit the real API (spending money, leaking data) and the run looked flaky. The app now **never auto-loads the cwd `./.env` when it detects it is running under a test runner** (`default_env_candidates` + `_running_under_test_runner`), so no test invocation can pick up the real key; the real application is unaffected (it still loads `./.env`). Also hardened the one test that genuinely leaked env vars (`test_runtime_env` now snapshots/restores `os.environ`). `tests/test_runtime_env.py` +3.

- **Deleting a document then re-adding it is no longer mistaken for a duplicate — and its data no longer lingers (deletion tombstone, default mode).** When you deleted a filed document and ran `rescan`, its catalog row was kept as a full `DELETED` record (name, path, fiche, hash). Re-dropping the same file in the inbox was then matched by hash and trashed as a "duplicate" — you could not re-file something you had deleted. Two changes fix this. (1) **De-duplication now ignores tombstones**: only a *live* (non-deleted) document counts as a duplicate, so a re-deposit is filed normally — and you're told, in the run, that you had previously deleted this content (logged as `redeposit_of_deleted`). (2) **A deleted row is reduced to a tombstone** — only its `id`, content `hash` and deletion date are kept; the name, path and the whole fiche are dropped, so nothing of a deleted document's content remains in the catalog (privacy), while the hash still lets a later re-deposit be recognised. On every deletion the leftover artifacts are quarantined, each to **its own library's trash**: the **mirror backup copy** and the **mirror's text sidecar → `Mirror_Trash`** (TTL-purged), and the **primary hidden text sidecar → the primary `Library_Trash`**. (Same rule for the `trash` command: the document and its primary sidecar go to `Library_Trash`, the mirror copy and mirror sidecar to `Mirror_Trash`.) The action log keeps a trace (`library_file_deleted`, `library_deleted_mirror_quarantined`, `library_deleted_mirror_sidecar_quarantined`, `library_deleted_sidecar_quarantined`). New `has_live_sha256` / `deleted_at_for_sha256` / `tombstone_document` on the catalog; new `_trash_deleted_artifacts`. (An opt-in `purge` mode that drops the hash + id too — for the rare case where even the fingerprint must not survive — is in the `deletion-mode` entry above.) `tests/test_catalog.py` +1, `tests/test_pipeline.py` +1, `tests/test_rescan.py` +1, `tests/test_library_trash.py` +1, `tests/test_sidecars.py` +1.

- **A file you send to `review` now also gets its hidden text sidecar.** The OCR/vision text exists only at read time, but the sidecar was written *after* a parked (decision-queue) file returned early — so a document you resolved via `review` had no sidecar and would be invisible to the upcoming deep search. The sidecar is now written before parking (next to the file in `Manual_Review`), and `resolve_pending_decision` moves it out with the document when you resolve. `tests/test_review.py` +1. 362 tests green.

- **rescan now syncs the catalogued NAME to your filename, so search shows the name you chose.** When you renamed a filed document by hand (e.g. `Facture_CAF` → `AR_CAF`), rescan updated the file's path but left the fiche's `name` at the AI's original value — so `search` displayed the stale name. rescan now ensures the fiche `name` follows the on-disk filename stem (the user's filename is authoritative for the catalogued name too, not just the path) — no AI, no re-reading. Runs on every rescan, fixing already-renamed files and all future renames; a no-op for files the app named. The document's keywords/content are untouched, so it stays findable by what it's about. CLI rescan summary gains `names synced`. `tests/test_rescan.py` +1. 354 tests green.

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
- **Two-phase set-aware organize** — a folder you drop in is catalogued, then placed as ONE coherent affair/series instead of scattering. A run is monotonic: it only ever files a document *deeper*, never de-organizes.
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
