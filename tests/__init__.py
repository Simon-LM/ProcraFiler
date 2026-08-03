"""Test-suite bootstrap: keep every routine run OFFLINE and DETERMINISTIC.

The CLI loads a `.env` at runtime (`procrafiler.runtime_env.load_runtime_env`,
called from `cli.main`), and its candidate list includes `./.env` — the
developer's real file with a live Mistral key + chains. Without this guard, any
test that drives the pipeline through the CLI would silently load that key and
hit the real Mistral API (the source of the "secret" calls we found).

We point `PROCRAFILER_ENV_FILE` at an empty file: it is the FIRST existing
candidate `load_runtime_env` finds, so it stops there and never reads the real
`.env`. Result: no key, no chains → the AI is never really called, the pipeline
falls back to manual review deterministically, and nothing leaves the machine.

Tests that DO exercise a real model set their own chains explicitly before the
run (e.g. `test_ollama_integration` points every task at local Ollama); those
win because they are already in `os.environ`.
"""

import os
from pathlib import Path

# Force the offline env file for the whole suite (override anything inherited).
os.environ["PROCRAFILER_ENV_FILE"] = str(Path(__file__).resolve().parent / "empty.env")

# The price refresh is the one thing in the app that reaches the network without an
# API key, so the `.env` guard above does not stop it: a run started by a test finds
# no refresh stamp, decides it is due, and calls GitHub. Switched off for the whole
# suite — the tests that exercise refreshing turn it back on and point it at a
# local URL or a mock, which wins because it is already in `os.environ`.
os.environ["PROCRAFILER_PRICING_REFRESH"] = "off"
