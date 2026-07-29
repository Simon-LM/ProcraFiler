<!-- @format -->

# Testing

> When a test fails, **start here**. This is the reference for how the suite is
> run, why it is offline, and what to check first.

## TL;DR — run the suite

```bash
make test          # routine suite: offline, deterministic, no API calls, free
make test-ollama   # opt-in: real LOCAL model integration (needs Ollama running)
make test-mistral  # opt-in: real MISTRAL API — COSTS MONEY (needs MISTRAL_API_KEY)
```

Always run from the **repo root**. `make test` is the canonical command — use it.
It runs `python -m unittest discover -t . -s tests`.

## The routine suite is OFFLINE by design

The routine suite must **never** call a real AI provider (no spend, no data
leaving the machine, deterministic results). Two independent layers guarantee it:

1. **Suite guard** — [`tests/__init__.py`](../tests/__init__.py) points
   `PROCRAFILER_ENV_FILE` at an empty env file, so the suite never loads your real
   `./.env` (no Mistral key, no model chains). This layer only runs when `tests`
   is imported as a **package**, i.e. with `-t . -s tests` (what `make test` does).
2. **App safety net** — the application refuses to auto-load the cwd `./.env` when
   it detects it is running under a test runner
   ([`runtime_env._running_under_test_runner`](../src/procrafiler/runtime_env.py)).
   This backstops layer 1 for **any** invocation: a bare
   `python -m unittest discover -s tests`, a single module, or a future `pytest`
   all stay offline.

Net effect: **no test invocation can reach the live Mistral API.** The real
application is unaffected — outside a test runner it still loads `./.env` normally.

## Why `-t .` matters (the gotcha, and its history)

`make test` uses `-t . -s tests`, not a bare `-s tests`. With `-s tests` alone,
`tests/` becomes the discovery top-level dir, modules import as `test_foo` instead
of `tests.test_foo`, and `tests/__init__.py` (layer 1) **never executes**.

Before the app safety net existed, that bypass meant CLI-driven tests loaded the
real `./.env` and "offline" unit tests intermittently hit the real Mistral API —
which showed up as a **flaky 5–6 failures** (e.g. an unexpected `.txt` sidecar
making a file-count assertion fail, depending on whether the network call
succeeded). Layer 2 now prevents that regardless of how you launch the tests, but
`make test` remains the canonical, documented command.

## The one thing offline tests cannot measure

Everything in the routine suite mocks the AI, so it proves a prompt is built and a
verdict is applied — never that the model **judges well**. The set-aware naming pass
exists to catch a photo whose vision reading went wrong; that judgement is only
measurable against a real model.

`make test-mistral` (opt-in, `PROCRAFILER_MISTRAL_IT=1`, costs money) is where it is
measured. Three judgements are checked there:

- **The naming pass.** No photos are needed: it never sees an image — a misread photo
  is an *input* to it, so supplying what a vision model would have produced reproduces
  the case exactly.
- **The `DOCUMENT: oui|non` marker**, which decides whether a photographed document is
  re-read with OCR. Two generated images, one of each kind.
- **The vision name hints.** One deliberately ambiguous image read under two different
  drop folders (the benefit), and one unambiguous image under a deliberately wrong
  filename (the contamination risk).

The assertions are on the **discrimination**, never on exact strings or the review
flag. Across real runs the same outlier came back as `Degats-eaux_pelouse-jardin` and
`Degats-eaux_tapis-salon`, separators drifted, and the review flag was set on one run
and not the next. What must hold is that a plausibly-misread photo joins its set while
a genuinely unrelated one does not. A test demanding an exact name would be red one
run in three and end up ignored — worse than no test.

The same rule decided how the name-hint test asserts. The tempting control — read the
ambiguous image with *no* hint and check the reading is neutral — is not stable: the
same JPEG came back "un motif abstrait" on one run and "de l'herbe ou un tissu" on the
next. Unhinted the model abstains *or* hedges, unpredictably. So the test asserts on
**exclusivity** instead: each hinted reading must name its subject and not mention the
rival one. Identical pixels cannot explain that, so it isolates the hint — and it costs
one fewer API call than the control would.

## Forcing an offline run by hand

`PROCRAFILER_ENV_FILE` is **authoritative**: the file it names is the only one
tried, and the search never falls through to `./.env` or the config-home files.
So the simplest way to run the real CLI with no key and no chains is:

```bash
PROCRAFILER_ENV_FILE=/dev/null procrafiler process-all --dry-run
```

`/dev/null` reads as empty, so nothing is configured. Any unreadable path (a typo)
loads nothing either, and `doctor` then **FAILs** rather than letting the run use
built-in defaults silently.

## Running a subset

```bash
python -m unittest discover -t . -s tests                     # whole suite
python -m unittest tests.test_pipeline                        # one module
python -m unittest tests.test_pipeline.TestPipeline.test_x    # one test
```

## When a test fails — checklist

1. **Run it the canonical way first:** `make test` (or `-t . -s tests`). Don't
   trust a bare `python -m unittest discover -s tests` for triage.
2. **Isolate it:** re-run just the failing test
   (`python -m unittest tests.<module>.<Class>.<test>`). If it passes alone but
   fails in the full run, suspect **cross-test state leakage** (usually
   `os.environ`) — see point 5.
3. **Signs an "offline" test reached the network** (a flaky `.txt` sidecar, a
   file-count off by one, a real OCR/vision result on a fake document): that was
   the `./.env`-isolation bug, fixed in PR #72. If it ever recurs, verify
   `runtime_env._running_under_test_runner()` still detects the runner and that
   `tests/__init__.py` still sets `PROCRAFILER_ENV_FILE`. **The suite must never
   load your real `./.env`.**
4. **The real API is only ever used by the opt-in `make test-ollama`** (local
   Ollama), never by the routine suite.
5. **If your test mutates `os.environ`**, snapshot and restore it (see
   [`tests/test_runtime_env.py`](../tests/test_runtime_env.py) `setUp`/`tearDown`)
   so it cannot leak into later tests.

## Adding tests

- Tests are **offline**. To exercise the AI path, mock it — patch
  `procrafiler.ai_analysis.call_mistral_chat` (see `tests/test_classification_pipeline.py`).
- Each test gets its own temp workspace via `PROCRAFILER_*` env vars in `setUp`.
- If you set any env var, restore it in `tearDown` (or use
  `unittest.mock.patch.dict`).

## Manual end-to-end testing (the sandbox)

`make test` covers the **automated, offline** suite. To exercise the real
pipeline by hand — against the live AI or in safe fallback mode — use the
isolated dev sandbox, which forces every path inside `sandbox/workspace/` and
never touches your real files:

```bash
./sandbox/run.sh e2e          # reset + init + seed samples + process-all + show result
```

See [`sandbox/README.md`](../sandbox/README.md) for the full step list.

## CI

[`.github/workflows/tests.yml`](../.github/workflows/tests.yml) runs `make test`
on every push and pull request, so the offline suite is always enforced the
canonical way.
