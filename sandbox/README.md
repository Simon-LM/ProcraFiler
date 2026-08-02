# ProcraFiler dev sandbox

A throwaway, **fully isolated** workspace to test ProcraFiler end to end without
any risk to your real files. `run.sh` forces every `PROCRAFILER_*` path inside
`sandbox/workspace/` itself, so a run here only ever reads/writes this folder —
it works out of the box with no `.env` path setup. The repo `.env` is read only
for the optional AI key + model chains.

Only the generated runtime workspace (`sandbox/workspace/`) is gitignored; the
runner (`run.sh`) and the synthetic `samples/` are versioned — the sandbox is a
test feature of the repo.

## Requirements: just `git` and `python3`

**You do not have to create a virtualenv.** On its first run, `run.sh` builds an
isolated `.venv/` in the repo and installs ProcraFiler into it (editable), then
reuses it on later runs. So a fresh clone is exactly two steps:

```bash
git clone https://github.com/Simon-LM/ProcraFiler.git && cd ProcraFiler
./sandbox/run.sh e2e
```

(An AI key is optional — see below.)

## One-shot end-to-end test

```bash
./sandbox/run.sh e2e
```

This resets the sandbox, initializes the layout, runs `doctor`, copies the
sample files into the Inbox, runs `process-all`, and prints the resulting tree.

- **Without a Mistral key** (default): the pipeline runs safely but the AI
  steps fall back, so files land in `Manual_Review`. This still proves the
  plumbing (move to queue, dedup, naming fallback, mirror, catalog, lock).
- **With a key** (set `MISTRAL_API_KEY` in the repo `.env`): files are read,
  named from content, and classified — the bank statement should land under
  `Personal/Administrative/Banking`, the EDF invoice under
  `Personal/Administrative/Utilities`, the personal note under `Personal/...`.
  This is the real end-to-end test.

## Individual steps

```bash
./sandbox/run.sh reset    # wipe sandbox/workspace
./sandbox/run.sh init     # create the folder layout
./sandbox/run.sh doctor   # diagnostics (paths, env, AI config, catalog, lock)
./sandbox/run.sh seed     # copy samples/ into the Inbox
./sandbox/run.sh run      # process-all
./sandbox/run.sh tree     # show inbox/queue/library state
./sandbox/run.sh log      # tail the actions log
```

Drop your own PDFs / scans / images into `sandbox/workspace/ProcraFiler_Inbox/Inbox/`
and run `./sandbox/run.sh run` to test the OCR / vision paths (needs a key).

**Any other ProcraFiler command** is passed straight through, so you can exercise
the whole surface against the sandbox, e.g.:

```bash
./sandbox/run.sh search facture
./sandbox/run.sh search-ai acoustique
./sandbox/run.sh language fr
./sandbox/run.sh status
```

## Why this is safe

- All paths are forced inside `sandbox/workspace/` by `run.sh`, so the run
  cannot reach your real files even if your `.env` says otherwise.
- ProcraFiler never deletes originals: duplicates move to `Inbox_Trash_Manual`,
  library removals move to `Library_Trash_Manual`. The only deletion is
  `purge-mirror-trash`, scoped to `Mirror_Trash`.
- `reset` only removes the generated `sandbox/workspace/` — your `samples/` and
  scripts stay.

## `reset` asks before deleting

`sandbox/workspace/` is gitignored: nothing `reset` deletes can be recovered, from git or anywhere else. So `reset` counts what is there and refuses to wipe a populated sandbox unless a human confirms at the prompt. A non-interactive caller — a script, an agent, CI — gets a refusal instead of a question it cannot answer. `FORCE=1 ./sandbox/run.sh reset` is the deliberate override.

This exists because an unattended `reset` once destroyed a real test corpus.
