# Canonical test commands — always run from the repo root.
#
# IMPORTANT: use `-t . -s tests` (not just `-s tests`). The `tests` package
# __init__ forces the suite OFFLINE (no Mistral key/chains loaded → the AI is
# never really called → deterministic, free). That guard only runs when `tests`
# is imported as a PACKAGE, which requires the top-level dir to be the repo root.

PYTHON ?= .venv/bin/python

.PHONY: test test-isolation test-ollama test-mistral

test:  ## Routine suite: offline, mocked, deterministic, free (no API)
	$(PYTHON) -m unittest discover -t . -s tests

test-isolation:  ## Prove the suite writes NOTHING into the user's home
	@set -eu; \
	fake=$$(mktemp -d); \
	log=$$(mktemp); \
	trap 'rm -rf "$$fake" "$$log"' EXIT; \
	if ! HOME="$$fake" XDG_DATA_HOME="$$fake/.local/share" XDG_CONFIG_HOME="$$fake/.config" \
			$(PYTHON) -m unittest discover -t . -s tests > "$$log" 2>&1; then \
		tail -40 "$$log"; \
		echo "FAIL — the suite itself did not pass under a redirected HOME"; \
		exit 1; \
	fi; \
	leaked=$$(find "$$fake" -mindepth 1 | head -50); \
	if [ -n "$$leaked" ]; then \
		echo "FAIL — the suite wrote into the home directory:"; \
		echo "$$leaked" | sed 's|^|  |'; \
		echo "Anything listed above would have landed in a real user's home."; \
		exit 1; \
	fi; \
	echo "OK — $$(grep -oE 'Ran [0-9]+ tests' "$$log" | tail -1), nothing written to the home directory"

test-ollama:  ## Opt-in: real LOCAL Ollama integration (needs Ollama running)
	PROCRAFILER_OLLAMA_IT=1 $(PYTHON) -m unittest tests.test_ollama_integration

test-mistral:  ## Opt-in: real MISTRAL API integration — COSTS MONEY (needs MISTRAL_API_KEY)
	PROCRAFILER_MISTRAL_IT=1 $(PYTHON) -m unittest tests.test_mistral_integration
