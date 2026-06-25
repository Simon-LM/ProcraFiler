<!-- @format -->

# Test plan — toward v1.0.0

The 1.0 stabilisation pass. Current suite: **467 offline tests** (56 files), plus an
opt-in local-AI suite (`PROCRAFILER_OLLAMA_IT=1`). This file tracks the tests we add
before tagging v0.8.0, then real-world testing, then v1.0.0.

Run: `make test` (offline) · `make test-ollama` (real local models).

## Already strong

AI analysis/naming/organize/grouping (mocked), rescan, review, document date, series
folders, subfolders, scrub/heal, verify-catalog, restore, backup (incl. encryption),
search (+ ai), doctor, catalog, user_setup, deletion mode, library trash, sidecars.

## 🔴 P1 — Install / Update / Uninstall scripts

The safety net behind "an update never forces you to reorganize your folders." Today
only `uninstall.sh` is tested (2). Subprocess tests against a temp `$HOME`, like
`tests/test_uninstall_script.py`.

- [x] **install.sh** — fresh install creates the venv + env file (`0600`) seeded from
      `.env.example`; the launcher runs + points at the env file; a re-install leaves an
      existing env file untouched; arg validation. **Done** (`test_install_script.py`,
      stub-python so no real pip). *(System mode left to the P4 manual pass — needs root.)*
- [x] **update.sh** — checks out the **latest release tag** (not a branch HEAD);
      **refuses a dirty clone** (doesn't move); "already on latest" no-op; missing
      metadata exits; **never touches user data**. **Done** (`test_update_script.py`, real
      tagged git repo + stub venv).
- [x] **Data survives an update** — covered: update never touches the user's data
      (script test) + catalog schema migration & settings forward-compat
      (`test_catalog.py` / existing regression tests).
- [ ] **uninstall.sh** — extend the existing 2 tests: `--purge` removes exactly the
      regenerable files and keeps the context file; `--mode system`.

## 🔴 P1 — End-to-end with local AI (Ollama, opt-in)

Expand `tests/test_ollama_integration.py` (currently 2).

- [x] text document read + classified (`gemma4:12b`).
- [x] image document vision-read + classified (`qwen2.5vl:7b`).
- [x] **OCR** on a scanned PDF end-to-end (`minicpm-v`). **Verified** (~113s).
- [x] **set-aware organize/grouping**: a dropped folder of related files runs the
      ORGANIZE pass and both are filed. **Verified** (~6.5 min).
- [x] **`process-all` on a mixed batch** (text + image + scanned PDF) — all three
      reading paths in one run, filed, catalogued. **Verified**. (Search-after-process
      is covered by the offline `test_search.py`, not asserted here.)
- [n/a] document date / naming-convention assertions on local output — deliberately
      NOT asserted: these e2e tests check the **plumbing** (no crash, filed/parked),
      not model quality, which varies by local model. Quality is judged manually.

> These run only with `PROCRAFILER_OLLAMA_IT=1` (`make test-ollama`); they are slow
> and load local models — never part of `make test`. `PROCRAFILER_AI_THROTTLE`
> (default 3 s here) paces the sequential calls.

## 🟠 P2 — Mirror & consistency

- [ ] **Mirror correctness** (mock AI): after `process-all` the mirror matches the
      library file-for-file (+ sidecars); after a `rescan` move/rename the mirror
      follows; `library-trash` moves the mirror copy to mirror trash.
- [ ] **Hand edit in the library** propagates to the mirror on the next run; deletion
      tombstone propagation.
- [ ] **Heal**: a corrupt library file is restored from the versioned mirror.
- [ ] **Conflict management = Phase 2 (reconcile not built yet)** → write WITH that
      feature: same doc edited in two places → conflict copy + `review`; adds from two
      inboxes deduped; delete-vs-edit resolved by timeline; cross-location reconcile.

## 🟡 P3 — Unit & CLI

- [ ] **CLI dispatch** (arg parsing + exit codes) for the thinly-covered commands:
      `scrub` / `verify-catalog` / `restore` / `backup`, `language`, `deletion-mode`,
      `features` / `feature-set`, `policy-effective`, `reindex`, `deleted-history`,
      `status` (incl. the durability + backup-reminder lines).
- [ ] **restore** re-rooting with tombstones / paths outside the library root.
- [ ] **backup**: empty library; the `.sha256`; a corrupted encrypted archive → clean error.
- [ ] **mirror.py**: version-quarantine / TTL edge cases.

## 🟢 P4 — Real-world manual checklist (the v0.8.0 → v1.0.0 gate)

Not automated — done by hand on a clean machine:

- [ ] `git clone` → `install.sh` → `procrafiler setup` (paths + AI + context).
- [ ] Drop real documents → `process-all` (local AI) → inspect the library.
- [ ] `search` / `search-ai` find them; `scrub` is clean; `backup --encrypt` + restore.
- [ ] `update.sh` to a newer tag → library intact. `uninstall.sh` → library kept.

---

**Order:** P1 install/update → P1 local-AI e2e → P2 mirrors → P3 CLI/unit. P1+P2 are
enough to tag **v0.8.0** with confidence, then the P4 manual pass gates **v1.0.0**.
