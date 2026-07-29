<!-- @format -->

# Dev must never write to production

> **Why this document.** On 2026-07-28 at 23:53:26 a development run created a full
> ProcraFiler layout in the user's real home directory — inbox, library taxonomy,
> mirror, state files. Nothing was lost (the layout was empty, the action log had
> zero lines), but nothing in the code prevented it either. The only thing standing
> between a dev session and a real library was operator discipline.
>
> That is not acceptable for a tool whose entire value is that you trust it with
> your documents. This document is the gated checklist that closes it.

## The rule, in one sentence

**A program started from the source tree must never write into the user's home.**

## Vocabulary (used consistently below)

| Term | Meaning, concretely |
| --- | --- |
| **production** | the *installed* app **and** the user's real documents |
| **development** | the source tree we work in, and its sandbox |
| **the paths** | the five roots the app computes at startup: workspace/inbox, queue, library, mirror, state |
| **installed interpreter** | the `VENV_DIR` recorded by the installer in `install-meta.env` |

Measured defaults, with `$HOME` substituted — this is what an unconfigured run targets:

```text
workspace_root   $HOME/Downloads/ProcraFiler_Inbox
queue_dir        $HOME/Downloads/ProcraFiler_Inbox/Queue
library_root     $HOME/ProcraFiler_Library
mirror_root      $HOME/ProcraFiler_Library_Mirror
state_root       $HOME/.local/share/procrafiler
```

**Nothing below hardcodes a path.** Every check is derived from the running
process and from markers on disk, so it protects any user who installs from
GitHub, not just this machine.

## Evidence gathered 2026-07-29

Probed with `HOME` redirected to a throwaway directory, counting files and
directories created:

| Operation | Entries created in the home |
| --- | --- |
| `import` of any module | 0 |
| `default_runtime_paths()` | 0 |
| `ensure_runtime_layout()` | **47** |
| **`procrafiler doctor`** | **48** |
| the whole offline test suite (645 tests) | **0** |

Two findings:

1. **The test suite is clean.** `make test` writes nothing outside its temporary
   directories. The incident did not come from the tests, and item **E** below
   freezes that property so it cannot regress silently.
2. **`doctor` — a diagnostic command — creates 48 directories.** `ensure_runtime_layout`
   is called by ~30 CLI entry points, including read-only ones. That is item **D**.

Forensics on the incident itself: the layout was created by a single sub-second
call at `23:53:26.83`; the library, mirror and manual trash held **zero files**;
`actions_log.jsonl` was touched but empty. No document was ever processed. The
exact command could not be identified with certainty from the logs — the strongest
candidate is an ad-hoc script run without the sandbox environment.

---

## Gate 1 — the three guards

Applied at the start of every **mutating** command, and only when the running
interpreter is **not** the installed one. Three independent reasons to refuse, so
that the failure of one does not open the door.

### [ ] A. Refuse a dev build that targets the installed layout

The installer already writes `~/.local/share/procrafiler/app/install-meta.env`
containing `VENV_DIR=`. Compare `sys.prefix` to it: if they differ, this is not
the installed build. If such a build resolves paths that match the installed
layout, **refuse**, naming the directory it was about to touch.

Deliberate override: `PROCRAFILER_ALLOW_REAL_DATA=1`.

> Note: git detection was considered and **rejected**. `install-meta.env` records
> `REPO_ROOT` pointing at this very source tree — production is installed *from*
> the git checkout and updates from it, so "am I in a git tree" distinguishes
> nothing at all.

**Done when.** A dev-build run against the installed layout exits non-zero with a
message naming both interpreters, and a test proves it.

### [ ] B. Refuse a dev build that targets a layout holding real work — unless it is a marked sandbox

"Library is not empty → refuse" is **wrong on its own**: `sandbox/workspace/`
legitimately holds 331 test files, and blocking it would break normal development.

So the sandbox marks itself. `sandbox/run.sh` drops a `.procrafiler-sandbox` file
in its workspace, symmetric to production's `install-meta.env`.

The rule becomes: **dev build + the target holds real work + no sandbox marker →
refuse**. "Holds real work" = at least one document under the library root, or a
non-empty `actions_log.jsonl`.

This is the net under the other two: it catches a production install whose marker
was deleted, or a library copied somewhere else.

**Done when.** For a dev build:

| Target | Marker | Contents | Decision |
| --- | --- | --- | --- |
| `sandbox/workspace/` | sandbox | 331 files | pass |
| the installed layout | production | anything | refuse (A) |
| a fresh explicit directory | none | empty | pass, and stamp the sandbox marker |
| an unknown explicit directory | none | holds documents | refuse (B) |
| *(nothing specified)* | — | — | refuse (C) |

### [ ] C. A dev build may only write to paths given explicitly

Reading may fall back to defaults. **Writing never.** If the interpreter is not
the installed one and no `PROCRAFILER_*` root (or `--workspace` / `--library`) was
supplied, refuse — the command has nowhere to write until it is told where.

This is the guard that needs neither a marker nor pre-existing data, so it is the
only one that works on a fresh clone on a fresh machine. **It is also the one that
would have stopped the 2026-07-28 incident.**

**Done when.** `.venv/bin/python -m procrafiler process-all` with no environment
refuses and prints how to run the sandbox instead.

---

## Gate 2 — supporting work

### [ ] D. A diagnostic must not create anything

Split `ensure_runtime_layout()` (mutating) from a read-only `resolve_runtime_layout()`.
`doctor`, `search`, `list` and every `--dry-run` use the latter. Measured today,
`doctor` creates 48 directories; a command you run to *decide whether to trust the
app* must not modify the machine.

**Done when.** `procrafiler doctor` against a virgin home creates 0 entries, with a
test asserting it.

### [ ] E. Freeze the leak detector as a permanent test

Run the suite with `HOME` (and `XDG_*`) redirected to a throwaway directory and
**fail if anything appears there**. Anything appearing in the fake home is
something that would have landed in the real one.

Currently green: 645 tests, 0 entries. This item is about making that permanent, so
a future change cannot reintroduce the incident unnoticed.

**Done when.** The check runs in CI and fails on a deliberate mutation that writes
to `Path.home()`.

### [ ] F. Guard `sandbox/samples/`

`sandbox/samples/` **is tracked by git** — three invented text files
(`M. Dupont Jean`, `rue des Lilas`, `ACME SARL`). It is the one place in the
repository where dropping a real document would push it to GitHub.

History was audited on 2026-07-29 and is **clean**: 5 files ever existed under
`sandbox/`, and **zero** PDF/JPG/PNG/DOCX/ODT/XLSX were ever committed anywhere in
the project. This item is prevention, not remediation.

**Done when.** A test fails if `sandbox/samples/` contains anything other than the
known text fixtures. Real material goes to `private/` (gitignored).

---

## Gate 3 — what is still owed on the AI side

### [x] G. Measure F3 on the real photos — **DONE 2026-07-29**

> 15 real photos, each read twice (blind, then with its filename and drop folder),
> 30 vision calls. The hint helps more on real photos than on generated ones, and no
> contamination was found. Full result in `docs/pre-prod-hardening.md` §F3; no
> prompt change was needed. The photos stay in `private/` and out of git, and nothing
> identifying was recorded in any tracked file.

The run also served as the first rehearsal of the rule this document exists to
enforce. It was executed with every `PROCRAFILER_*` root pointed at a throwaway
directory, and the home was listed before and after:

```text
HOME modifié ? NON, rien créé
sandbox jetable : 0 entrées
```

Zero entries in either — the measurement only calls the reader, which touches no
layout. That is the discipline items A–C exist to make automatic rather than
remembered.

---

## Housekeeping

- [ ] Remove the empty directories created in the home on 2026-07-28
      (`Personal/`, `Work/`, `Manual_Review/` under `~/ProcraFiler_Library`, and the
      empty state files). Nothing in them; awaiting the user's go-ahead.
- [ ] **PR #116** is open and awaiting merge. Per the one-branch-at-a-time rule, the
      work above starts from `main` once it lands.
