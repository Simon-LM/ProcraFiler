# Canonical test commands — always run from the repo root.
#
# IMPORTANT: use `-t . -s tests` (not just `-s tests`). The `tests` package
# __init__ forces the suite OFFLINE (no Mistral key/chains loaded → the AI is
# never really called → deterministic, free). That guard only runs when `tests`
# is imported as a PACKAGE, which requires the top-level dir to be the repo root.

PYTHON ?= .venv/bin/python

.PHONY: test test-ollama

test:  ## Routine suite: offline, mocked, deterministic, free (no API)
	$(PYTHON) -m unittest discover -t . -s tests

test-ollama:  ## Opt-in: real LOCAL Ollama integration (needs Ollama running)
	PROCRAFILER_OLLAMA_IT=1 $(PYTHON) -m unittest tests.test_ollama_integration
