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

## Status — all gates closed (2026-07-29)

Every item is done: **A** (#110), **B**/**C**/**E** (#111), **F1/F2/F4** (#112, #113),
**D** (#114), **G** (#115), **F3** (#116). F3 had been deferred on the grounds that
naming the file to the vision model would contaminate the read; measuring it showed
the contamination does not occur and that some images are undecidable without the
hint, so the deferral was reversed.

**What this does NOT close:** none of it proves the AI *judges well*. The tests
assert on prompts and on file operations; whether a misread photo now lands
correctly needs a real run on real files. That is the remaining unknown, and no
offline test can retire it.

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

### [x] B. `restore` silently overwrites newer library documents — **DONE**

> **Shipped.** `restore` now computes a **plan** before touching anything:
> `--dry-run` prints what would be created / overwritten / left alone and changes
> nothing; a destructive restore **prompts** (`--yes` to skip, for scripts); and each
> overwritten document is **moved to `Library_Trash_Manual`** (relative path kept, a
> second restore does not clobber the first rescue) instead of being destroyed —
> consistent with the app's own never-delete rule. Documents present only in the
> library are reported as untouched, so the user knows a restore **merges** rather
> than replaces. Covers `--from` and `--from-archive` (the archive path delegates to
> the same function). Tests: [`tests/test_restore_safety.py`](../tests/test_restore_safety.py)
> (7 tests); mutation-verified.
>
> **Deviation from the original fix note:** the prompt triggers on *"would overwrite
> a differing document"*, not on *"library is non-empty"* as first written. A
> non-empty library that the source does not touch is not at risk, and blocking it
> would train the user to type `y` reflexively — the prompt must mean something.

<details><summary>Original finding (kept for the record)</summary>

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

</details>

### [x] C. No validation that the configured paths are not nested — **DONE**

> **Shipped.** `config.layout_conflicts` is the single shared validator (resolved
> paths, so `~/./lib/` cannot smuggle a nested root past it), covering the roots the
> user never types — the library trash and the app state — which is exactly where an
> innocent choice bites. `setup` now **refuses** a nested layout and re-asks (up to
> 3 attempts) instead of printing a warning nobody reads; `doctor` **FAILs** on an
> existing broken layout, because `setup` only ever validated at creation time and a
> config can be hand-edited into the env file. Tests:
> [`tests/test_layout_conflicts.py`](../tests/test_layout_conflicts.py) (16 tests),
> including one asserting the shipped defaults pass their own guard;
> mutation-verified (9 failures).

<details><summary>Original finding (kept for the record)</summary>

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

</details>

### F. Weight the original filename as a **strong** hint, per read path — **F1/F2/F4 DONE**

> **Verified offline, NOT verified in production.** The tests assert on the built
> prompt — that the framing flips with `read_via` and that the sibling names arrive.
> They cannot prove the *model obeys*. Whether a misread photo now lands correctly
> needs a real run on real files; that is the one part of F no offline test can close.

**The principle (clarified 2026-07-25).** "Never trust the filename" means the name
must never *decide* — it does **not** mean discarding it. A proposed name stays a
**strong indicator**, and it must weigh **more** the less reliable the extracted
content is. This is the correct reading of the IA-first rule, not an exception to it.

**What already works — do not "fix" it.** The filename *is* already passed to the
analysis prompt ([`ai_analysis.py:142-153`](../src/procrafiler/ai_analysis.py#L142-L153))
with sound framing (*"HINTS, NOT ground truth — the content is authoritative"*),
alongside `source_folder`. The gaps below are about **weighting** and **missing
signals**, not about introducing the hint.

#### [x] F1. Pass `read_via` into the analysis prompt and vary the hint weight — **DONE**

> **Shipped.** `read_via` is threaded from `_read_and_analyze` through
> `analyze_content` into `_build_hints_block`, which now emits two different
> framings. Mechanical read (`text`): unchanged — *"the content is authoritative;
> use these only to disambiguate"*. AI read (`vision` / `ocr`): the block states the
> text **may be incomplete, misread or invented**, that the filesystem facts are
> **RELIABLE**, that specific evidence beats a vague description, and that a clear
> contradiction should return `category_path: null` with alternatives (→ decisions
> queue) rather than guess from the image.

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

#### [x] F2. Pass the sibling filenames of the dropped set into the per-file prompt — **DONE**

> **Shipped.** Both entry points pass them, and the difference matters: `process-all`
> uses the **pre-computed** set member list, because by the time file N is analysed
> files 1..N-1 have already MOVED to the Queue — a live Inbox scan would only ever
> see the tail of the set. `process-once` scans the file's own Inbox subfolder, where
> its set-mates are still sitting. Capped by count (12) **and** total length (400
> chars), so a 200-file folder cannot swamp the prompt or the bill. Loose Inbox-root
> files are singletons and get no sibling line — no invented context.

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

#### [x] F3. Hint the vision reader itself — **DONE**

> **Shipped 2026-07-29**, after the deferral below was overturned by measurement.
> `read_with_vision` now receives the original filename and the drop folder, and
> `build_vision_prompt` renders them as *"indices de provenance"* granted exactly one
> power — **breaking a tie on an ambiguous image** — and explicitly denied the power
> to add content: declared fallible, possibly meaningless or plainly wrong, with an
> instruction to contradict them when the image does. The block is inserted **before**
> the `DOCUMENT: oui|non` question, which must stay the last instruction.
> Tests: 12 in `tests/test_vision_name_hints.py` + 2 real-API in
> `tests/test_mistral_integration.py`; mutation-verified (sending the Queue's name
> instead of the user's fails 1, moving the block after the DOCUMENT question fails 3,
> dropping the caveat fails 1, removing the arguments fails 5).

`read_with_vision` receives only the path and a generic French prompt, so the vision
model gets no hint at all. Passing the filename could steer transcription — but
**risks leading the model into confirming a wrong name**, contaminating the very
signal we wanted independent of the content.

**Decision (2026-07-25): not adopted.** F1 now handles the same problem *after* the
read, where a wrong name cannot contaminate the transcription. Leaving the read blind
also keeps the two signals independent, which is what makes weighing them against
each other meaningful in the first place. Revisit only if real runs show vision
misreads that F1 fails to catch — this checkbox stays open as that trigger.

**Reversal (2026-07-29).** The deferral rested on two assumptions, and measuring both
on the real API (`mistral-medium-latest`) settled them:

1. *"F1 handles the same problem after the read."* It does not, for a class of images
   where the read itself is undecidable. The same green-texture JPEG, unhinted, came
   back as *"un fond ou un motif abstrait"* on one run and *"de l'herbe ou un tissu"*
   on the next. There is no misreading for a later pass to correct — there is no
   reading at all. Hinted, the model commits: *"un tapis ou une moquette"* under
   `Degats-eaux-salon`, *"une pelouse bien tondue ou un gazon dense"* under
   `Jardin-printemps`. Identical pixels; the difference is the hint.
2. *"The name will contaminate the transcription."* The stated risk, tested head-on: a
   textless garden scene named `facture-EDF-mars-2026.jpg` in a folder `Factures-2026`.
   The model answered *"un cercle orange sur un fond vert"*, `DOCUMENT: non`, and used
   none of "facture", "EDF", "montant", "euro". The caveat holds; the contamination did
   not occur. Stable across three consecutive runs.

**Confirmed on real photographs (2026-07-29).** The measurements above use synthetic
images, so they were re-run on 15 real photos of a water-damage claim (kept outside
the repository), each read twice — blind, then with its filename and drop folder.
The effect is larger than on generated images, because a real photo is genuinely
harder to read:

| Blind reading | With the two names |
| --- | --- |
| "the inside of an open washing machine, the drum" | "under a kitchen unit, electrical cables, **water-damage marks**" |
| invented a text string that is not in the image, "renovation in progress" | "no visible text […] **signs of water damage**, mould" |
| "plumbing or **irrigation**" | "under a unit, kitchen […] damp, mould, **water damage**" |
| "a piece of white material" | "**plasterboard**, debris from works or damage" |

Two failure modes disappeared with the hint: an object identified as something it is
not, and **invented text**.

**The contamination control held.** A photo showing a plank on grass — nothing to do
with the claim — read identically with and without the hint. The model did not
project the folder's subject onto it.

**A stronger negative result on the contamination question.** The drop folder name
contained a place name that genuinely appears on one of the documents. Under the hint
the model also returned that document's **postcode**, which was *not* in the hint and
could only have come from the page. So the hint had made it read *more* of the
document — the handwritten party block, which the blind reading skipped entirely —
rather than echo the folder name. The one field it got wrong was a handwritten street
name, which is a vision-model weakness on cursive, not a hint effect.

Incidentally this also validated the OCR-confirm feature above on real material: all
four photographed claim forms came back `DOCUMENT: oui`, so in a real run their text
would come from the OCR model rather than from this description.

#### [x] F4. Correct the README and spec wording — **DONE**

> Both restated: the name **never decides**, but it is always a hint whose weight
> rises as content reliability falls. README §"How it works" and §"AI Analysis", and
> spec §1.1. This wording is what made the audit itself misread the design, so it was
> not a cosmetic fix.

The README (*"the original filename survives only as a deterministic last-resort
fallback"*) and the spec §1.1 (*"never a trusted input"*) **overstate** the distrust
and no longer match the code — this wording is what caused the audit itself to
misread the design. Restate both as: *the name never decides, but it is always a hint
whose weight rises as content reliability falls.*

---

## P2 — polish

### [x] D. `PROCRAFILER_ENV_FILE=/dev/null` does not force offline — **DONE**

> **Shipped**, and fixed at the root rather than at the symptom. The bug was not
> `/dev/null` specifically: it was that an EXPLICIT `PROCRAFILER_ENV_FILE` could be
> silently ignored in favour of another source. Naming a file is a deliberate
> instruction, so it is now **authoritative** — the only candidate, no fall-through.
> Readability is decided by actually reading, not by `is_file()`, so `/dev/null`
> (a character device) loads as empty and stops the search. A typo'd path now loads
> nothing instead of quietly adopting the developer's `./.env`, and `doctor` **FAILs**
> on it. Tests: 5 in `tests/test_runtime_env.py`; mutation-verified (restoring
> `is_file()` fails the `/dev/null` test, restoring the fall-through fails 3).

`/dev/null` fails the `is_file()` test
([`runtime_env.py:78`](../src/procrafiler/runtime_env.py#L78)), so it is skipped and
the app **falls through to the cwd `./.env`**. Verified: the real Mistral key and
chains get loaded. A direct trap for the "isolate every run" habit — an empty file
works, `/dev/null` does not.

**Fix.** Accept non-regular files as an explicit override, or add an unambiguous
`PROCRAFILER_NO_ENV=1`. Log the effective source at startup (`status` already shows
`env_loaded_from` — make it impossible to miss when a *fallback* occurred).

### [x] E. `doctor` is too optimistic — **DONE**

It verified that folders exist and are writable, but none of the states that
actually matter before trusting the app. All three are now checks, so `doctor`
earns its role as the go/no-go command:

- **non-empty Queue** → FAIL, naming the stranded files (shipped with **A**);
- **nested roots** → FAIL, one line per conflict (shipped with **C**);
- **mirror on the same disk** as the library → WARN (shipped with **C**) — `setup`
  said this once at creation and never again.

---

## Final gate

### [x] G. Full test audit — guarantee no file is ever lost or corrupted

> **Re-audited 2026-07-29, after v0.9.0**, with line coverage plus **16 deliberate
> mutations** of safety-critical code. Twelve were killed by the existing suite;
> **four survived**, and they shared one blind spot: the failure injection added
> here targets the *pipeline's* library write, and nothing had ever injected a
> fault into `scrub --repair` or into the mirror's staging cleanup — the paths that
> only run when something is already going wrong. Closed by
> `TestFailureDuringTheRepairItself` and `TestMirrorFailurePathsCleanUpAfterThemselves`
> (5 tests; `scrub` 93 % → 97 %, `mirror` 85 % → 94 %). All four mutations now die.
>
> Two things the re-audit cleared rather than fixed: all 24 `patch()` targets in the
> suite were checked for the inert-patch trap (a symbol patched at its defining
> module while the caller imported it by name) — **none are inert**; and
> `scrub._restore`'s internal hash guard is unreachable from its only callers, which
> pass an already-verified source. That is defensive code, not a hole.
>
> Three gaps were left open at that point. Two were then closed, after re-judging
> them on consequence rather than on how the code looked:
>
> - **`restore.format_plan`'s rendered text.** First filed as "low risk, the data it
>   renders is tested". That was the wrong question. This text is printed
>   *immediately before* the `[y/N]` prompt of an irreversible overwrite — it is the
>   basis on which the user consents, not decoration. Now covered by 6 tests: the
>   three counts cannot be swapped, every document at risk is named, a list longer
>   than 20 still accounts for the hidden ones, a harmless restore raises no false
>   alarm, and both locations are shown. Five mutations die. (Mitigating, and worth
>   recording: the number inside the prompt itself comes straight from
>   `plan.overwrites`, so a rendering bug misleads about *which* documents, not
>   *how many*.)
> - **`collapse_nesting`'s directory-merge branch.** It moves real documents when an
>   inner folder collides with a same-named folder in the parent — a different code
>   path from the file-collision case, which was the only one tested. Now covered by
>   3 tests: both documents survive a merge, the recursion still refuses to clobber a
>   file, and a folder left non-empty by a conflict is kept rather than removed with
>   the document inside. Three mutations die.
>
> **Deliberately left, with the reason:** naive-datetime normalisation
> (`dt.replace(tzinfo=utc)` in `mirror` and `naming`). The worst outcome is a few
> hours of drift on a 30-day mirror-trash retention — no document is lost, nothing
> is misfiled, and a test would mostly assert that Python's `datetime` behaves. This
> is a decision, not an oversight; revisit only if retention ever becomes
> short enough for hours to matter.

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

- [x] **Conservation invariant (the key test).** For N input files and an interrupt
      injected at *every* pipeline step in turn: after the interrupt **and** after the
      next run, every input is accounted for exactly once across
      {Inbox, Queue, Library, Inbox_Trash, Manual_Review} — never zero, never twice.
      Parameterise over the interruption point rather than hand-picking one.
- [x] **Crash-during-recovery.** Interrupt the Queue recovery of A itself; assert the
      following run still converges (idempotence).
- [x] **`SIGKILL` end-to-end**, not just a raised exception — an in-process exception
      does not prove durability against a real kill.
- [x] **Content integrity, not just presence.** Assert the sha256 of every filed
      document equals the input's — the current suite largely checks paths and counts,
      which cannot detect a truncated or half-copied file.
- [x] **Failure injection** on `move`/`copy2` (`ENOSPC`, `EACCES`, `EIO`) at each
      write site: library placement, mirror sync, trash move, sidecar write. Assert
      no partial file is ever left presented as complete.
- [x] **Interrupted mirror sync** leaves a truncated mirror copy → `scrub` detects it
      and `--repair` heals it from the library.
- [x] **Nested-path rejection** (C), for every pair, via `setup` and via `doctor`.
- [x] **`restore` safety** (B): refuses/prompts on a non-empty library; `--dry-run`
      mutates nothing; overwritten documents are recoverable.
- [x] **Adversarial filenames** survive a full round trip, including the action log
      staying valid JSONL.
- [x] **Two concurrent `process-all` processes** on one Inbox: one proceeds, the other
      is refused, and no file is processed twice or lost.
- [x] **Hint weighting** (F1/F2): the `vision`/`ocr` prompt differs from the `text`
      prompt in the authority it grants the content; sibling names reach the per-file
      prompt. Offline — assert on the built prompt string, never a live call.
- [x] **Sidecar/document coupling**: no state leaves a `.txt` sidecar orphaned or
      pointing at the wrong document after a move, rename or interrupt.

**Done when.** Every item above is covered, `make test` stays offline and
deterministic, and the conservation invariant is enforced by a test that fails if
anyone later reintroduces a path where a file can go missing.

> **DONE.** 614 tests, offline and deterministic. Six items were already closed by
> the fixes above (conservation invariant, crash-during-recovery, content integrity,
> nested paths, restore safety, hint weighting); the remaining six landed in
> [`tests/test_durability_audit.py`](../tests/test_durability_audit.py) and
> [`tests/test_durability_processes.py`](../tests/test_durability_processes.py).
>
> **Findings:** no new defect. 13 adversarial filenames (embedded newline, CR, tab,
> quotes, unicode, RTL override, trailing dot/space, 200 chars, shell
> metacharacters) all survive a full round trip with the action log staying valid
> JSONL — the JSON encoder and the stem sanitiser already covered it. Injected
> ENOSPC / EACCES / EIO at the library write leave the document intact in the Queue
> and report an error rather than a clean run; a failing mirror copy does not cost
> the primary. `scrub` detects and heals a truncated, missing or bit-rotted copy in
> both directions, and refuses to "repair" when both copies are bad.
>
> **Two test defects of my own were found and fixed while writing this**, which is
> the argument for the exercise: a sidecar test patched a symbol the pipeline does
> not use (so it proved nothing), and the sidecar-follows-a-move test moved the
> sidecar by hand — testing the test, not `rescan`. Both now assert the real thing.
>
> `SIGKILL` and concurrency use real subprocesses: an in-process `KeyboardInterrupt`
> still unwinds the stack and runs `finally` blocks, so it cannot stand in for a
> power cut. Mutation-verified: disabling Queue recovery fails the `SIGKILL` test.
