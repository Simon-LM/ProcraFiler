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
| **source checkout** | this package imported from `<root>/src/procrafiler` next to a `<root>/pyproject.toml` — i.e. a development build, as opposed to the installer's copy in `site-packages` |

## Status (2026-07-29)

**Every item is done.** Gate 1 (guards A, B, C), gate 2 (D, E, F), gate 3 (G).
**G** — measuring the vision name hints on real photographs — was completed before
the guards, and is the reason this document exists at all: running the app on real
material is what exposed the gap.

**What this does NOT close:** these guards live *inside the application*. They stop
a source checkout, and any script that drives the pipeline, from writing where it
should not. They cannot stop `rm -rf` typed into a shell — nothing in the code can.
That boundary belongs to whatever runs the commands.

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

## Gate 1 — the three guards — **CLOSED**

Applied at the start of every **mutating** command, and only when the package was
imported from a source checkout. Three independent reasons to refuse, so that the
failure of one does not open the door.

> **All three shipped**, in [`src/procrafiler/dev_guard.py`](../src/procrafiler/dev_guard.py),
> called from `ensure_runtime_layout()` — the single function that ~30 CLI entry
> points and any ad-hoc script go through, and the one that created the layout in
> the incident. 14 tests in [`tests/test_dev_guard.py`](../tests/test_dev_guard.py);
> six mutations verified (removing the call fails 1, never detecting a checkout
> fails 7, dropping each individual guard fails 1–2, dropping the sandbox marking
> fails 1). Reproducing the incident command now refuses, naming the four
> directories it declined to touch.

### How "is this a development build?" is answered

Not by `sys.prefix`, and **not** by looking for a `.git` directory, which was tried
and is useless: `install-meta.env` records `REPO_ROOT` pointing at this very
checkout, because production is installed *from* the git tree and updates from it.

What does separate them: the installer **copies** the package into its own venv's
`site-packages`, while development uses `pip install -e`. So the question becomes
*where was this module imported from* — a package at `<root>/src/procrafiler` next
to a `<root>/pyproject.toml` is a checkout; anything else is not.

This matters beyond tidiness: it is what keeps a user who runs a plain
`pip install procrafiler` from ever meeting these guards. A guard that fires on a
real user is worse than no guard, because it gets switched off.

### [x] A. Refuse a dev build that targets the installed layout — **DONE**

The installer writes `~/.local/share/procrafiler/app/install-meta.env`, which names
the `ENV_FILE` holding the user's own configuration. The guard reads that file and
computes the roots the **installation actually uses** — so a user who moved their
library out of `$HOME` is protected where their library really is, not where the
defaults would have put it.

Deliberate override, for all three guards: `PROCRAFILER_ALLOW_REAL_DATA=1`.

### [x] B. Refuse a dev build that targets a layout holding real work — unless it is a marked sandbox — **DONE**

"Library is not empty → refuse" is **wrong on its own**: `sandbox/workspace/`
legitimately holds hundreds of test files, and blocking it would break normal
development.

So the sandbox is marked, in **two** places, and both turned out to be necessary:

- `ensure_runtime_layout()` stamps `.procrafiler-sandbox` itself whenever a source
  checkout creates a layout that is neither production nor already in use. Nothing
  to remember, and it covers every throwaway layout a test creates — which is why
  the 645 existing tests kept passing unchanged.
- `sandbox/run.sh` stamps it too. Found by running the real sandbox against the new
  guard: `sandbox/workspace/` **predates** the marker and already held hundreds of
  test documents, so the guard refused it as "a library that already holds
  documents". Auto-stamping only helps a layout created *after* the guard existed.
  A test asserts the script and the guard agree on the filename.

The rule: **dev build + the target holds real work + no sandbox marker → refuse**.
"Holds real work" = at least one file under the library root, or a non-empty
`actions_log.jsonl`. Deliberately *not* a comparison against a pristine manifest:
that would need maintaining and would drift with every taxonomy change. An empty
taxonomy skeleton answers "no", which is what keeps a fresh layout usable.

This is the net under the other two: it catches a production install whose marker
was deleted, or a library copied somewhere else.

For a dev build:

| Target | Marker | Contents | Decision |
| --- | --- | --- | --- |
| `sandbox/workspace/` | sandbox | hundreds of files | pass |
| the installed layout | production | anything | refuse (A) |
| a fresh explicit directory | none | empty | pass, and stamp the sandbox marker |
| an unknown explicit directory | none | holds documents | refuse (B) |
| *(nothing specified)* | — | — | refuse (C) |

### [x] C. A dev build may not write to the built-in defaults — **DONE**

Reading may fall back to defaults. **Writing never.**

**Changed during implementation:** the spec asked "were `PROCRAFILER_*` supplied?".
The check is on the **resolved roots** instead — "are these the roots an
unconfigured run would target?" — which is equivalent for the incident but does not
break callers that build a `RuntimePaths` directly instead of going through the
environment. Same intent, expressed on paths rather than on how they were obtained.

This is the guard that needs neither a marker nor pre-existing data, so it is the
only one that works on a fresh clone on a machine with no installation and no
documents. **It is also the one that stops the 2026-07-28 incident**, verified by
re-running the original call:

```text
ProductionWriteRefused: Refusing to write to the INSTALLED ProcraFiler layout
from a source checkout.
  inbox   /home/…/Downloads/ProcraFiler_Inbox
  library /home/…/ProcraFiler_Library
  mirror  /home/…/ProcraFiler_Library_Mirror
  state   /home/…/.local/share/procrafiler
  running   /media/…/ProcraFiler/.venv
  installed /home/…/.local/share/procrafiler/app/.venv
```

---

## Gate 2 — supporting work — **CLOSED**

### [x] D. A diagnostic must not create anything — **DONE**

> **Shipped.** Seven read-only commands (`status`, `features`, `policy-effective`,
> `doctor`, `search`, `search-ai`, `deleted-history`) create **0 entries** against a
> layout that does not exist. 9 tests in
> [`tests/test_diagnostics_readonly.py`](../tests/test_diagnostics_readonly.py);
> five mutations verified.

**The split was not needed.** The spec called for a read-only
`resolve_runtime_layout()` beside the mutating one. In practice a read-only command
needs no layout function at all — `default_runtime_paths()` already gives it the
paths. The fix was to stop calling the mutating one, plus make two collaborators
stop creating:

- **the runtime lock.** `check_runtime_lock` *acquired the real lock* to report
  whether it was free, which creates the state directory and the lock file — and
  briefly blocks any run starting at that moment. New `probe_runtime_lock()` opens
  the existing file without `O_CREAT`; a missing file means nobody holds it.
- **the search index.** It is created next to the catalog, so under a missing state
  directory `search` died with `unable to open database file`. It now answers
  "no catalog yet — nothing has been filed", and `search-ai` decides that *before*
  paying for an AI query expansion.

**What this recovers.** `_check_path_writable`'s `FAIL "missing: {path}"` branch was
**unreachable by construction**: the caller created every path a line earlier. So
`doctor` could not tell you a library had disappeared — an unmounted disk, a deleted
folder, a mistyped path. Now:

```text
$ procrafiler doctor          # library configured to a typo'd path
[FAIL] library_root: missing: /home/…/typo-in-my-path
$ ls /home/…/typo-in-my-path
No such file or directory     # …and it was not created
```

**One refinement found while testing.** "Never set up" and "something disappeared"
are different problems. A first run reported nine identical failures, which is noise
rather than an answer. When *every* root is missing, `check_paths` now returns a
single actionable line:

```text
[FAIL] layout: not created yet — run `procrafiler setup` (or `init-layout`).
       Expected the inbox at … and the library at …
```

A **partially** missing layout is the alarming case and is still reported root by
root. One existing test in `tests/test_user_setup.py` asserted the old shape and was
updated: it builds the roots it needs so that the case it actually tests — an
installed layout with the mirror disabled — is the one it exercises.

### [x] E. Freeze the leak detector as a permanent test — **DONE**

`make test-isolation` re-runs the whole suite with `HOME` and `XDG_*` redirected to
a throwaway directory and fails if anything appears there. It runs in CI on every
push and pull request, after `make test`.

The suite passing says nothing about *where* it wrote; this says it.

```text
OK — Ran 663 tests, nothing written to the home directory
```

Mutation-verified: a test that does `(Path.home() / "ProcraFiler_Library").mkdir()`
makes the target fail and names the leaked path.

### [x] F. Guard `sandbox/samples/` — **DONE**

`sandbox/samples/` **is tracked by git** — three invented text files
(`M. Dupont Jean`, `rue des Lilas`, `ACME SARL`). It is the one place in the
repository where dropping a real document would push it to GitHub.

History was audited on 2026-07-29 and is **clean**: 5 files ever existed under
`sandbox/`, and **zero** PDF/JPG/PNG/DOCX/ODT/XLSX were ever committed anywhere in
the project. This item is prevention, not remediation.

[`tests/test_repo_hygiene.py`](../tests/test_repo_hygiene.py) fails if
`sandbox/samples/` holds anything beyond the known fixtures, if any of them is a
real document format, or if the `private/` and `sandbox/workspace/` ignore rules
are ever dropped from `.gitignore`.

---

## Gate 3 — what was owed on the AI side — **CLOSED**

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
