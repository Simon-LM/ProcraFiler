<!-- @format -->

# Instructions for AI coding agents

Rules for any agent working on this repository. They are not style preferences —
each one exists because ignoring it did damage.

## 1. Never operate outside this repository

**Do not read, list, glob, stat, copy, execute or modify anything outside the
ProcraFiler repository root.** This includes, and is not limited to:

- `~`, `/home`, `/usr`, `/opt`, `/etc`
- `~/.config`, `~/.local`, `~/Documents`, `~/Downloads`
- any binary living outside the repo — **including a read-looking
  `--version` or `--help`**. An option that looks harmless is not a guarantee
  about a program you did not build.

An existence test is still looking: `ls -d ~/Procra*` is as forbidden as `cat`.

Permitted: the repository tree, the agent's own scratch/temp directory, and the
agent's own memory store.

**This is a prohibition, not a procedure.** It is lifted only by the maintainer's
**explicit authorisation, granted per command, after they have read the exact
command**. Propose the command written exactly as it would run, then leave it
unexecuted. Approval of one command approves that command and nothing else —
never a batch, never a follow-up the agent judges equivalent.

If a question can only be answered from outside the repo, say so and stop. Most of
the time the question does not need answering: verify the claim that prompted it
first, because the usual cause is an agent trying to substantiate something it
should simply retract.

## 2. Never run the app with its default paths

ProcraFiler's default runtime paths point into a real home directory —
`~/Downloads/ProcraFiler_Inbox`, `~/ProcraFiler_Library`,
`~/.local/share/procrafiler`, `~/.config/procrafiler`. Running the CLI "bare"
creates and writes real folders there.

Run it **only** through `./sandbox/run.sh`, which forces every `PROCRAFILER_*`
path into the gitignored `sandbox/workspace/`. Never run `scripts/install.sh`.

Two traps worth knowing:

- `scripts/uninstall.sh --purge` honours `PROCRAFILER_HOME` and
  `PROCRAFILER_CONFIG_HOME` from the environment. Run in a shell where those are
  exported, it purges whatever they point at.
- `runtime_env.default_env_candidates()` falls through to
  `~/.config/procrafiler/procrafiler.env` when no `./.env` is present, so a
  command run from the wrong directory can silently load a different config.

## 3. Never commit, push, tag or merge without being asked

Ask first — every time, including for a change the agent is certain about.
Releases, tags and GitHub Releases are the maintainer's decision.

## 4. One branch at a time

Finish a branch, open its PR, ask for the merge, and **wait**. Never open a second
branch while one is in flight. Every PR carries its own `CHANGELOG.md` entry under
`[Unreleased]`, following the existing section order (Added → Changed → Fixed →
Security).

## 5. English for everything that lands in the repository

Commit messages, PR titles and bodies, code comments, docstrings, documentation.
The repository is public: never commit personal data, real names, addresses, or
identifying details from real test documents — anonymise every example.

## 6. Tests

`make test` runs the suite offline (1000+ tests). `make test-isolation` proves the
run wrote nothing into the home directory. Read `docs/testing.md` before
attributing a failure to flakiness. Mutation testing is the quality signal that
matters here: a test that survives having its subject deliberately broken is not
protecting anything.
