<!-- @format -->

# Pre-production hardening checklist

> **Why this document.** The app has been validated in the sandbox, never on real
> files. A sandbox run exercises the **nominal path**; it does not exercise what
> happens when a run does not finish, or when the user mistypes a path. Every item
> below came from an audit dated **2026-07-25** (518 tests passing at the time) and
> was **reproduced**, not inferred. The repro commands are included so each fix can
> be verified against the same evidence.
>
> The stake is not code elegance: the product's entire value rests on trust. One
> document silently lost and the user never puts their files back in.

## How to use this list

- Items are grouped by **gate**, not by area: `P0` blocks the first real run, `P1`
  blocks recommending the tool to anyone else, `P2` is polish.
- Each item states **Symptom → Evidence → Fix → Done when**, so "done" is testable
  rather than a matter of opinion.
- The last item (**G**) is a global test audit and is deliberately last: it can only
  be completed once A–F have settled.

---

## P0 — blocking before the first real run

### [x] A. Recover files stranded in the `Queue` — **DONE** (branch `fix/queue-recovery-crash-safety`)

> **Shipped.** `recover_queue` runs first in both `process-once` and `process-all`,
> restoring each stranded file to its **original Inbox subfolder** (read from the
> `move_to_queue` action-log event, so dropped SETS are not scattered), idempotent
> under a crash during recovery, reported in the batch summary, and `doctor` now
> FAILs (exit 1) while the Queue is non-empty. Covered by
> [`tests/test_crash_recovery.py`](../tests/test_crash_recovery.py) (14 tests,
> including the conservation invariant over 9 interruption points).
>
> **Verified by mutation testing** — the fix was deliberately reverted to confirm the
> tests actually detect its absence (a passing test proves nothing on its own):
> neutralising `recover_queue`'s **call sites** makes 3 test methods fail (reported as
> 11 failures, because the conservation invariant counts its 9 subtests separately);
> neutralising **the function itself** fails all 14. Both mutations were reverted; the
> suite is green.
>
> **Also fixed here — found while implementing, not in the original audit:**
> library placement was **not atomic**. `shutil.move` degrades to copy+unlink across
> filesystems, and `setup` actively recommends separate disks, so an interrupted
> placement could leave a **truncated document at its final library path**, which the
> next `rescan` would ingest as a genuine new file — silent corruption. Placement now
> stages into a hidden fixed-length temp file in the destination directory and
> `os.replace`s it (atomic); leftovers are swept. Same fix applied to the mirror copy
> (cross-device by design), which additionally protected `restore` from ever seeing a
> truncated mirror file as the good copy.
>
> **Known trade-off:** a recovered file is re-read from scratch, so it costs its AI
> call again. Deliberate — resuming mid-flight would require persisting the analysis
> state, and a fresh read is always correct.

<details><summary>Original finding (kept for the record)</summary>

**Symptom.** A file leaves the Inbox for `Queue/`
([`pipeline.py:1306`](../src/procrafiler/pipeline.py#L1306)) **before** the AI read.
Nothing ever looks in `Queue/` again — the only other references to `queue_dir` in
`src/` are a `mkdir` and a `print` of the path. An interrupted run therefore leaves
documents in a folder no code path revisits: no longer in the Inbox (so never
re-processed), not in the library (so invisible to `rescan`), absent from the
catalog, and unmentioned by `doctor`.

**Evidence.** Simulating `Ctrl-C` during the 3rd file of a 3-file dropped folder:

```text
*** run interrupted (Ctrl-C) ***
Inbox   : 0 files
Queue   : 3 files   ← scan_0.txt  scan_1.txt  scan_2.txt
Library : 0 files
```

A subsequent clean run, and `doctor`, both report success:

```text
Batch result: processed: 0, duplicates: 0, manual_reviews: 0, errors: 0
doctor → 22 checks: 19 OK, 3 WARN, 0 FAIL, 0 SKIP
Queue still holds: scan_0.txt  scan_1.txt  scan_2.txt
```

**The app reports "all good" while three of the user's documents are invisible.**

**Why this is likely, not theoretical.**

- AI calls are **slow** — minutes per file with a local Ollama model. Interrupting a
  batch is ordinary, not exceptional.
- The README explicitly invites it: *"live logs so the user can watch and interrupt"*.
- In the two-phase flow **every** file of a set moves to the Queue before any is
  filed, so interrupting a 50-file folder orphans up to 50 documents at once.
- Same outcome for any hard stop: `SIGKILL`, OOM, power loss, closed SSH session.

The code already knows the hazard — two comments in `_catalog_one_inbox_file` say
*"NEVER leave it stranded in the Queue"* for the **logical** branches. Only the
crash case is unhandled.

**Fix.**

1. On startup of every `process-*`, **recover the Queue first**: for each file
   found there, either return it to the Inbox for a clean re-run, or resume it.
   Recovery must be idempotent and must survive a crash *during recovery*.
2. Reconcile against the catalog: a queued file whose sha256 is already filed was
   interrupted *after* filing — do not duplicate it.
3. Add a `doctor` check that **FAILs** on a non-empty Queue, naming the files.
4. Report recovered files in the batch summary, so recovery is never silent.

**Done when.** An interrupt (exception, and `SIGKILL`) at *any* pipeline step leaves
every input file either in the Inbox, the Library, a Trash, or a Queue that the next
run recovers — never unaccounted for; and `doctor` refuses to report a clean bill of
health while the Queue is non-empty.

</details>

**Still open from A:** the `SIGKILL` end-to-end test (the suite covers in-process
interrupts; a real kill is listed under **G**).

---

## P1 — before recommending the tool to anyone else

### [ ] B. `restore` silently overwrites newer library documents

**Symptom.** `restore` is a *recovery* command that is itself destructive when
misused. It copies over existing files
([`restore.py:76`](../src/procrafiler/restore.py#L76)) with **no confirmation, no
`--dry-run`, and no backup of what it overwrites**.

**Evidence.** Library holds the user's current edit, mirror holds an older copy:

```text
library BEFORE restore : 'CURRENT library version the user cares about'
library AFTER  restore : 'OLD mirror version'
files_copied: 1
```

**The inconsistency is the tell:** the **catalog DB is backed up** before being
replaced (`catalog_backup`), but the **user's documents are not**. Meanwhile
`uninstall.sh --purge` asks for confirmation over *regenerable* state, while
`restore` asks for nothing over *irreplaceable* documents.

Realistic path to harm: the user tries `restore` on an old mirror "just to check it
works" and silently rolls their library back.

**Fix.** Add `--dry-run` (list what would be overwritten, with hash differences);
require confirmation when the target library is non-empty; add `--yes` for scripting;
and move each overwritten document to `Library_Trash_Manual` rather than destroying
it — consistent with the app's own never-delete rule.

**Done when.** No `restore` invocation can destroy a document without either an
explicit confirmation or a recoverable copy in a trash folder.

### [ ] C. No validation that the configured paths are not nested

**Symptom.** `setup` invites free-form paths and only checks **exact distinctness**
([`user_setup.py:289-294`](../src/procrafiler/user_setup.py#L289-L294)) — a `set()`
equality test. **Nesting is never detected**, yet the code assumes it cannot happen,
in writing, at [`rescan.py:71`](../src/procrafiler/rescan.py#L71): *"The library's
trash and the mirror live OUTSIDE `library_root` already"* — true of the defaults
only.

**Evidence.** With `Library=~/Documents` and `Mirror=~/Documents/Mirror` (a plausible
thing to type), the library walk swallows the mirror:

```text
walk_library_files sees:
   Mirror/Personal/Administrative/Banking/…__Statement.pdf   ← mirror copy
   Personal/Administrative/Banking/…__Statement.pdf          ← the original
```

Measured consequences: mirror files get **renamed** by rescan (a
`Statement__quarantined_2026-01-01.txt` in `Mirror_Trash` became
`2026-07-25_…__Statement_quarantined_…`), **phantom catalog rows** appear (2 spurious
out of 4), an extra `Mirror/Mirror/` level is created, and — with real chains
configured — **one paid AI call per unknown mirror file**.

Mitigating fact: it **converges** rather than exploding (sha256 dedup catches the
repeats), so this is silent corruption and waste, not runaway growth.

**Fix.** In `setup`, **refuse** (not merely warn) any pair where one path contains
another — Inbox, Library, Library_Trash, Mirror, state dir — comparing resolved
paths. Add the same check to `doctor`, which currently never re-validates the
configuration; include the "mirror on the same disk as the library" check there too
(`setup` warns once at creation and never again).

**Done when.** A nested configuration is impossible to create through `setup`, and
`doctor` FAILs on one that already exists on disk.

### F. Weight the original filename as a **strong** hint, per read path

**The principle (clarified 2026-07-25).** "Never trust the filename" means the name
must never *decide* — it does **not** mean discarding it. A proposed name stays a
**strong indicator**, and it must weigh **more** the less reliable the extracted
content is. This is the correct reading of the IA-first rule, not an exception to it.

**What already works — do not "fix" it.** The filename *is* already passed to the
analysis prompt ([`ai_analysis.py:142-153`](../src/procrafiler/ai_analysis.py#L142-L153))
with sound framing (*"HINTS, NOT ground truth — the content is authoritative"*),
alongside `source_folder`. The gaps below are about **weighting** and **missing
signals**, not about introducing the hint.

#### [ ] F1. Pass `read_via` into the analysis prompt and vary the hint weight

`grep read_via src/procrafiler/ai_analysis.py` returns **nothing**. The prompt
therefore asserts *"the content is authoritative"* identically whether the text came
from a PDF text layer (literal bytes, genuinely authoritative) or from a vision model
describing a blurry photo (an AI guess that can hallucinate). **This is the inversion
to fix:** when the "content" is itself an interpretation, the filename must carry
*more* weight, not the same.

- Thread `read_via` (`text` / `ocr` / `vision`) through `_read_and_analyze` into
  `analyze_content` and `_build_analysis_prompt`.
- `text` → keep the current wording (content authoritative).
- `vision` / `ocr` → state that the transcription may be unreliable, and that the
  filename and folder are **corroborating evidence**; a clear conflict between a
  confident filename and a vague visual description should favour the filename, or go
  to the decisions queue rather than guess.

**Done when.** The hint's authority is an explicit function of `read_via`, covered by
a test asserting the vision prompt and the text prompt differ in that wording.

#### [ ] F2. Pass the sibling filenames of the dropped set into the per-file prompt

Only the *folder name* is passed today. The names of the **other files dropped
alongside** are a real, free signal that is currently thrown away — the strongest case
being a photo among clearly-named documents (the user's own example).

The `organize` pass does **not** cover this: it works on fiches **already produced**,
so the misreading has already happened upstream at per-file analysis. This must be
fixed at the analysis step or not at all.

- The set members are already grouped in `process_all_inbox_files` (`work_sets`), so
  the sibling names are available before Phase 1 analysis — no new plumbing to gather
  them.
- Cap their count and total length to bound token cost.

**Done when.** A file analysed inside a set receives its siblings' names as context,
with a test proving a photo in a folder of clearly-named documents is classified using
that context.

#### [ ] F3. (Optional — decide on real files) Hint the vision reader itself

`read_with_vision` receives only the path and a generic French prompt, so the vision
model gets no hint at all. Passing the filename could steer transcription — but
**risks leading the model into confirming a wrong name**, contaminating the very
signal we wanted independent of the content.

**Do not adopt by default.** The safer stance is to keep the read blind and weight the
hint afterwards (F1). Revisit only if real runs show vision misreads that F1 fails to
catch.

#### [ ] F4. Correct the README and spec wording

The README (*"the original filename survives only as a deterministic last-resort
fallback"*) and the spec §1.1 (*"never a trusted input"*) **overstate** the distrust
and no longer match the code — this wording is what caused the audit itself to
misread the design. Restate both as: *the name never decides, but it is always a hint
whose weight rises as content reliability falls.*

---

## P2 — polish

### [ ] D. `PROCRAFILER_ENV_FILE=/dev/null` does not force offline

`/dev/null` fails the `is_file()` test
([`runtime_env.py:78`](../src/procrafiler/runtime_env.py#L78)), so it is skipped and
the app **falls through to the cwd `./.env`**. Verified: the real Mistral key and
chains get loaded. A direct trap for the "isolate every run" habit — an empty file
works, `/dev/null` does not.

**Fix.** Accept non-regular files as an explicit override, or add an unambiguous
`PROCRAFILER_NO_ENV=1`. Log the effective source at startup (`status` already shows
`env_loaded_from` — make it impossible to miss when a *fallback* occurred).

### [ ] E. `doctor` is too optimistic

It verifies that folders exist and are writable, but none of the states that
actually matter before trusting the app: **non-empty Queue** (A), **nested paths**
(C), **mirror on the same disk** as the library. This is the command a user runs to
decide whether to trust the tool — it should carry those checks.

---

## Final gate

### [ ] G. Full test audit — guarantee no file is ever lost or corrupted

**Do this last**, once A–F have settled. Two parts: (1) re-read the existing suite
for gaps, (2) add the missing tests. The bar is not coverage percentage — it is:
**can any sequence of failures make a document disappear or change silently?**

Audit findings that seed the work (measured 2026-07-25, 518 tests):

- **No interrupt/crash test exists at all.** `grep -rl "KeyboardInterrupt\|SIGINT\|SIGKILL"
  tests/` returns nothing. Six tests assert the Queue is empty — all **after a
  successful run** ([`test_pipeline.py:165`](../tests/test_pipeline.py#L165),
  `test_batch_cli.py:49`, `test_dry_run_cli.py:62`, `test_runtime_lock.py:87`, …).
  The happy path is well covered; the interrupted path is not covered anywhere.
- **No failure-injection tests in the pipeline.** `ENOSPC` (disk full mid-move or
  mid-mirror-copy), `PermissionError`, a read-only destination, an I/O error during
  hashing. Matches only exist in `test_install_script`/`test_update_script`/`test_doctor`.
- **No adversarial filename tests.** Names >255 bytes, embedded newlines (which would
  also corrupt the JSONL action log), emoji, RTL overrides, trailing dots/spaces.
- **No real concurrency test.** The lock raises `RuntimeLockedError`
  ([`test_runtime_lock.py:50`](../tests/test_runtime_lock.py#L50)), but two genuine
  concurrent `process-all` processes racing on one Inbox are never exercised.

Tests to add:

- [ ] **Conservation invariant (the key test).** For N input files and an interrupt
      injected at *every* pipeline step in turn: after the interrupt **and** after the
      next run, every input is accounted for exactly once across
      {Inbox, Queue, Library, Inbox_Trash, Manual_Review} — never zero, never twice.
      Parameterise over the interruption point rather than hand-picking one.
- [ ] **Crash-during-recovery.** Interrupt the Queue recovery of A itself; assert the
      following run still converges (idempotence).
- [ ] **`SIGKILL` end-to-end**, not just a raised exception — an in-process exception
      does not prove durability against a real kill.
- [ ] **Content integrity, not just presence.** Assert the sha256 of every filed
      document equals the input's — the current suite largely checks paths and counts,
      which cannot detect a truncated or half-copied file.
- [ ] **Failure injection** on `move`/`copy2` (`ENOSPC`, `EACCES`, `EIO`) at each
      write site: library placement, mirror sync, trash move, sidecar write. Assert
      no partial file is ever left presented as complete.
- [ ] **Interrupted mirror sync** leaves a truncated mirror copy → `scrub` detects it
      and `--repair` heals it from the library.
- [ ] **Nested-path rejection** (C), for every pair, via `setup` and via `doctor`.
- [ ] **`restore` safety** (B): refuses/prompts on a non-empty library; `--dry-run`
      mutates nothing; overwritten documents are recoverable.
- [ ] **Adversarial filenames** survive a full round trip, including the action log
      staying valid JSONL.
- [ ] **Two concurrent `process-all` processes** on one Inbox: one proceeds, the other
      is refused, and no file is processed twice or lost.
- [ ] **Hint weighting** (F1/F2): the `vision`/`ocr` prompt differs from the `text`
      prompt in the authority it grants the content; sibling names reach the per-file
      prompt. Offline — assert on the built prompt string, never a live call.
- [ ] **Sidecar/document coupling**: no state leaves a `.txt` sidecar orphaned or
      pointing at the wrong document after a move, rename or interrupt.

**Done when.** Every item above is covered, `make test` stays offline and
deterministic, and the conservation invariant is enforced by a test that fails if
anyone later reintroduces a path where a file can go missing.
