<!-- @format -->

# Backlog & things to revisit later

A running checklist of deferred work and open questions. Not a spec — a place to
park ideas and observations so nothing is lost between sessions.

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

- [ ] **SUPERVISOR** — the optional AI control pass over ambiguous outputs (spec §9).
- [ ] **VIDEO + audio analysis** — phase 2, after the rest is validated on real files.
- [ ] **Automatic processing** — a watcher (systemd user service + inotify) or a cron
      job, so dropping a file in the Inbox processes it without a manual command.
      Today processing is on-demand only (`process-once` / `process-all`).
- [ ] **`library-untrash`** — a restore command (currently restore is a manual `mv`).
- [ ] **`rescan`** — the pure-secretary sync, AUTOMATIC before every run (and standalone):
      a file the user placed by hand in the library is READ IN FULL (complete fiche into
      the catalog, for future search) and gets the timestamp prefix (the user's stem is
      untouched). It tracks every file by its content fingerprint (sha256), so ANY hand
      reorganization is followed without AI: a file renamed or moved, or a whole FOLDER
      renamed or moved (every file inside keeps its sha256), just has its catalog path/name
      updated — no re-reading, no re-classification, no re-naming. A deleted file is flagged.
      This is the supported way to fix an awkward auto-named folder (e.g. rename `CV_LM` →
      `CV` and rescan follows). The AI *understands*, it never *decides* — the user's
      location and name always win.
      **Duplicate policy (decided 2026-06-13):** a hand-placed file whose sha256 already
      exists in the catalog is a deliberate copy — rescan CATALOGS it anyway (reusing the
      original's fiche, zero AI call), ALERTS ("duplicate of `<original path>`" in the
      summary + a `library_duplicate_detected` action-log event), and NEVER acts on it —
      no deletion, no symlink substitution, no reorganization. The Inbox/library asymmetry
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
