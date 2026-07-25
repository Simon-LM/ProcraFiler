<!-- @format -->

# Backlog & things to revisit later

A running checklist of deferred work and open questions. Not a spec — a place to
park ideas and observations so nothing is lost between sessions.

> **Before the first real-files run, see
> [pre-prod-hardening.md](pre-prod-hardening.md)** — the reproduced findings of the
> 2026-07-25 audit (Queue orphaning on interrupt, `restore` overwriting newer
> documents, unvalidated nested paths, filename-hint weighting) plus the test-audit
> gate. That checklist gates production use; this file stays the general idea park.

## Classification — ambiguous cases to study

The goal: **collect real ambiguous cases before designing a fix.** One example is
not enough to choose the right solution; several will reveal the pattern.

The recurring tension so far: the **Work vs Personal** split (now the top-level
binary in the taxonomy) is not in the document — it depends on the user's
*relationship* to it (hobby vs job), which the content alone can't reveal.

> Cases below are described generically/anonymized — never log the user's real,
> identifying document details in this public repo.

| # | Type of document | AI chose | Expected | Why it's ambiguous |
| --- | --- | --- | --- | --- |
| 1 | User manual for a piece of specialized/professional-grade equipment used as a hobby | `Work` | `Personal` | The equipment is professional-grade, so the model read the manual as professional. But the owner uses it as a hobby → personal. The content is identical either way; only the user's context decides. |
| 2 | Press article (photo of a newspaper page) about a local institution related to one of the user's hobbies | `Education` | `Hobbies`-ish (genuinely unclear) | A news clipping is not a personal document at all — the taxonomy has no natural home for press/miscellaneous. User judged this one objectively hard and NOT a serious error; collect more press/misc cases before deciding (a `Personal/Press`-style folder? leave to Hobbies by subject?). |
| 3 | Professional-practice workshop visual (a method of the user's declared trade) | `Personal/Hobbies` | `Work` | Mirror image of case 1: the user's context file declares the trade, so trade-related practices/events should lean Work. Addressed by a prompt weighting rule (the declared profession's documents prefer Work); keep watching. |

(Add new rows as we find more cases — keep them generic.)

### Candidate solutions (decide once we have several cases)

1. **User context in the classification prompt** — a short, configurable profile
   (e.g. "audio and IT are hobbies for me, not my job → Personal"). Likely fixes
   most Work/Personal cases for minimal cost. Strongest lever.
2. **Route ambiguous Work/Personal to the decisions queue** — when the model
   hesitates between work and personal, don't guess; surface it via `review`.
3. **Set-aware organize pass** — the two-phase organizer (Mistral medium, sees the
   whole folder + dates) has more context than per-file classification to resolve
   work-vs-personal, especially when files arrive grouped in a folder.

## Deferred features (planned, not built yet)

- [ ] **Selectable interface language (i18n)** — the guided first run is English-only
      today. Offer the interface in **French** first (the prompts were French before
      and the translation map exists in git history), then make it extensible to other
      languages. The user's content language (`language` / setup-context) is already
      independent of the UI language.
- [ ] **Data-durability / bit-rot protection** — for an archive of rarely-touched files.
      NOT blind periodic rewriting: instead an **integrity scrub** (periodically re-hash
      files and compare to the catalog's stored hash) + **heal from the mirror** (restore
      a corrupted copy from the good one), and **SMART** (smartctl) for a real "disk is
      aging" warning rather than a calendar timer. **Required before v1.0.0.** Full
      architecture (destinations: local/LAN/rclone cloud · mirror vs encrypted cold
      backup · selective by capacity · cloud inbox + read-mirror to avoid conflicts · 3
      copies · self-contained restorable units): see [durability.md](durability.md).
- [ ] **SUPERVISOR** — the optional AI control pass over ambiguous outputs (spec §9).
- [ ] **VIDEO + audio analysis** — phase 2, after the rest is validated on real files.
- [ ] **Automatic processing** — a watcher (systemd user service + inotify) or a cron
      job, so dropping a file in the Inbox processes it without a manual command.
      Today processing is on-demand only (`process-once` / `process-all`).
- [ ] **`library-untrash`** — a restore command (currently restore is a manual `mv`).
- [x] **`rescan`** — the secretary sync, AUTOMATIC before every run (and standalone). **SHIPPED**
      (`procrafiler.rescan` + `run_rescan` + `_ingest_new_library_file` + CLI `rescan`/`deleted-history`).
      **Phase 1** (no AI): path-first matching (a still file is never hashed), moves/renames (incl.
      whole folders) → catalog repointed, deletions → row kept marked `DELETED` + logged (no alert) +
      `deleted-history` command, deliberate duplicates → catalogued reusing the original's fiche +
      untouched, re-deposits → `DELETED` row revived. **Phase 2**: a brand-new hand-placed file is
      READ IN FULL (fiche into the catalog, for search), gets the timestamp prefix (date AND time;
      the user's stem untouched), and a recurring kind is descended into its `<Entity>/<Year>/`
      subfolder like the run — anchored at the user's folder (never re-classified). Unreadable kinds
      are timestamped + catalogued with an empty fiche, never sent to manual review. (In-place CONTENT
      edits — same name, new bytes — are out of scope by design.)
      **PRESERVE ZONES (index-only):** a git repository (a dir with a `.git`) OR an `Archive` folder
      (`Personal/Archive`, `Work/Archive`) is left UNTOUCHED as a unit — never renamed/moved/dated
      (that would break it / defeat the point) — but its readable documents (media types text/pdf,
      under a size ceiling) are READ INTO THE CATALOG in place for search (`indexed_in_place: true`),
      so the contents are findable. `.git` internals and hidden files are never read. Archive folders
      are scaffolded + visible but EXCLUDED from the AI's classifiable categories (archiving is the
      user's act, never the AI's — avoids a catch-all magnet). Auto-dating backup folders was
      deliberately NOT done — it's reorganization; the user names a backup with a date themselves and
      rescan preserves it. (No note file is dropped in Archive — documented in README.md instead.)
      It tracks every file by its content fingerprint (sha256), so ANY hand
      reorganization is followed without AI: a file renamed or moved, or a whole FOLDER
      renamed or moved (every file inside keeps its sha256), just has its catalog path/name
      updated — no re-reading, no re-classification, no re-naming.
      This is the supported way to fix an awkward auto-named folder (e.g. rename `CV_LM` →
      `CV` and rescan follows). The AI *understands*, it never *decides* — the user's
      location and name always win.
      **Duplicate policy (decided 2026-06-13, refined run-17):** a hand-placed file whose
      sha256 already exists in the catalog is a deliberate copy — rescan CATALOGS it anyway
      (reusing the original's fiche, zero AI call), ALERTS ("duplicate of `<original path>`"
      in the summary + a `library_duplicate_detected` action-log event), and is never
      DEDUPLICATED — no deletion, no symlink substitution, no reorganization. It DOES receive
      the timestamp prefix though (horodatage is the app's, applied to every library file).
      The Inbox/library asymmetry
      is deliberate: an Inbox file is *to be processed* (duplicate → trash), a library file
      is the user's sovereign choice (duplicate → reported, untouched). Today such a file
      is simply invisible (sha256 dedup is Inbox-only; the catalog has no unique constraint
      on sha256, so two rows can share a hash — no migration needed).
- [ ] **`refile <path>`** (idea, decide on real usage) — the only escape hatch for asking
      the AI to reconsider an ALREADY-FILED file: unpin it from the catalog and re-ingest
      it through the normal Inbox pipeline. Needed because an already-catalogued file
      cannot simply be dropped back in the Inbox (sha256 dedup would trash it as a
      duplicate). NOT a global `reorganize` — that command was deliberately dropped:
      the Inbox is the only door through which the AI decides.
- [ ] **End-of-run corrective pass** — REJECTED by default (it is the discarded
      "file-then-fix" Option A in disguise; we harden the run's steps instead). Reevaluate
      only if several real runs show the in-flow filing staying insufficient.

## Open evaluations

- [ ] **Real-key smoke test of OCR (`/v1/ocr`) and vision contracts** — the text path
      is validated live (classification + naming). Scanned-PDF OCR and image vision
      still need a real scanned PDF / photo to confirm the request/response shapes.
- [ ] **`mistral-small` on images** — the user noted it can also process images, with
      unknown reliability. Worth comparing against `mistral-medium` to see if it can
      sometimes replace it (cost/quality tradeoff).
- [ ] **Re-test set grouping in the sandbox** — the per-folder organize rules (one
      destination per dropped folder, high bar to split out a file, no fragmentation
      across base categories, single affair name/period) are implemented in the
      organize prompt + pipeline; confirm on a real run that a multi-file affair
      (e.g. a water-damage claim) is no longer scattered across Housing/Insurance or
      multiple dates. (Moved here from the now-deleted plan-b.md.)
