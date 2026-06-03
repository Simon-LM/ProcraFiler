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

## Open evaluations

- [ ] **Real-key smoke test of OCR (`/v1/ocr`) and vision contracts** — the text path
      is validated live (classification + naming). Scanned-PDF OCR and image vision
      still need a real scanned PDF / photo to confirm the request/response shapes.
- [ ] **`mistral-small` on images** — the user noted it can also process images, with
      unknown reliability. Worth comparing against `mistral-medium` to see if it can
      sometimes replace it (cost/quality tradeoff).
