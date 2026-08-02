#!/usr/bin/env bash
# ProcraFiler dev sandbox runner — isolated end-to-end testing.
#
# Everything happens inside sandbox/workspace/: this script forces all
# PROCRAFILER_* paths there itself, so running it NEVER touches your real
# Downloads or home — no .env path setup required. The repo .env is read only
# for the (optional) AI key + model chains.
#
# Usage:
#   ./sandbox/run.sh e2e      # reset + init + doctor + seed samples + process-all + show result
#   ./sandbox/run.sh seed     # copy sample files into the sandbox Inbox
#   ./sandbox/run.sh run      # process-all on whatever is in the Inbox
#   ./sandbox/run.sh doctor   # diagnostics
#   ./sandbox/run.sh tree     # show the resulting library + inbox state
#   ./sandbox/run.sh log      # tail the actions log
#   ./sandbox/run.sh reset    # wipe sandbox/workspace AND recreate an empty layout (Inbox ready)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="$REPO/.venv/bin/python"

# AI config (API key + model chains) is read from the repo .env if it exists.
# It is OPTIONAL: with no key the pipeline still runs in safe fallback mode.
if [[ -f "$REPO/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi

# Isolation guarantee: force EVERY PROCRAFILER_* path inside sandbox/workspace/,
# overriding whatever the .env may set. A sandbox run can therefore only ever
# touch this folder — never your real ~/Downloads or home. This is what makes it
# safe, and it works out-of-the-box for any contributor (no .env path setup).
WORK="$HERE/workspace"
export PROCRAFILER_WORKSPACE_DIR="$WORK/ProcraFiler_Inbox"
export PROCRAFILER_LIBRARY_DIR="$WORK/ProcraFiler_Library"
export PROCRAFILER_LIBRARY_MIRROR_DIR="$WORK/ProcraFiler_Library_Mirror"
export PROCRAFILER_HOME="$WORK/state"
export PROCRAFILER_CONFIG_HOME="$WORK/config"

# Claim this workspace as a sandbox, so the dev guard lets it through once it
# fills up with test documents (see src/procrafiler/dev_guard.py). The app also
# stamps this itself on a layout it creates, but a workspace that predates the
# guard — or one restored from a backup — has no marker and would be refused as
# "a library that already holds documents".
mkdir -p "$PROCRAFILER_HOME"
if [[ ! -f "$PROCRAFILER_HOME/.procrafiler-sandbox" ]]; then
  cat > "$PROCRAFILER_HOME/.procrafiler-sandbox" <<'MARKER'
This layout is the ProcraFiler development sandbox and holds test data.
Delete this file if it ever becomes a real library.
MARKER
fi

WS="$PROCRAFILER_WORKSPACE_DIR"
LIB="$PROCRAFILER_LIBRARY_DIR"

procra() { "$PY" -m procrafiler.cli "$@"; }

# First-run bootstrap: make sure the repo virtualenv exists and has procrafiler
# installed, so a fresh clone can run the sandbox with a SINGLE command — no venv
# to create or activate by hand. It's a fast no-op once the venv is ready.
ensure_env() {
  if [[ ! -x "$PY" ]]; then
    echo "First run: creating an isolated virtualenv in .venv/ (one-time setup)…"
    python3 -m venv "$REPO/.venv"
    "$REPO/.venv/bin/python" -m pip install --quiet --upgrade pip
    "$REPO/.venv/bin/python" -m pip install --quiet -e "$REPO"
    echo "Virtualenv ready."
  elif ! "$PY" -c 'import procrafiler' 2>/dev/null; then
    echo "Installing procrafiler into the existing .venv…"
    "$PY" -m pip install --quiet -e "$REPO"
  fi
}

# `reset` is an `rm -rf` over the whole sandbox. That is fine on an empty layout
# and destructive on a populated one — and the sandbox is gitignored, so nothing
# it deletes can be recovered. It has already cost a real person their test corpus.
#
# So: count what is actually there, and refuse to destroy it unless a human says
# so at the prompt. A non-interactive caller (a script, an agent, CI) gets a
# refusal rather than a question it cannot answer — which is the whole point.
# FORCE=1 is the deliberate override.
sandbox_file_count() {
  find "$WS" "$LIB" "$PROCRAFILER_LIBRARY_MIRROR_DIR" -type f 2>/dev/null \
    | grep -v '/\.procrafiler-sandbox$' | grep -cv '/procrafiler\.lock$' || true
}

confirm_wipe() {
  local count; count="$(sandbox_file_count)"
  [[ "$count" -eq 0 ]] && return 0
  [[ "${FORCE:-}" == "1" ]] && { echo "FORCE=1 — wiping $count file(s)."; return 0; }
  if [[ ! -t 0 ]]; then
    echo "refusing to reset: $count file(s) in the sandbox, and nothing here can be" >&2
    echo "recovered (sandbox/workspace/ is gitignored). Re-run with FORCE=1 to wipe." >&2
    exit 3
  fi
  echo "This deletes $count file(s) from the sandbox. They are NOT recoverable."
  read -r -p "Type 'wipe' to confirm: " answer
  [[ "$answer" == "wipe" ]] || { echo "Cancelled — nothing deleted."; exit 3; }
}

wipe_sandbox() {
  rm -rf "$WS" "$LIB" "${LIB}_Trash_Manual" "$PROCRAFILER_LIBRARY_MIRROR_DIR" \
         "$PROCRAFILER_HOME" "$PROCRAFILER_CONFIG_HOME"
}

seed() {
  procra init-layout >/dev/null
  cp -n "$HERE"/samples/* "$WS/Inbox/" 2>/dev/null || true
  echo "Seeded $(ls -1 "$WS/Inbox" | wc -l) file(s) into the Inbox."
}

tree_view() {
  echo "== Inbox =="; find "$WS/Inbox" -type f 2>/dev/null | sed "s|$WS/||" || true
  echo "== Queue =="; find "$WS/Queue" -type f 2>/dev/null | sed "s|$WS/||" || true
  echo "== Library =="; find "$LIB" -type f 2>/dev/null | sed "s|$LIB/||" | sort || true
}

cmd="${1:-help}"
# Everything except plain help needs procrafiler installed in the venv.
if [[ "$cmd" != "help" && "$cmd" != "-h" && "$cmd" != "--help" ]]; then
  ensure_env
fi

case "$cmd" in
  help|-h|--help)
    cat <<'USAGE'
ProcraFiler dev sandbox — isolated end-to-end testing (never touches real files).

Usage: ./run.sh <command> [args...]

Orchestration (sandbox-only helpers):
  e2e      reset + init + doctor + seed samples + process-all + show result
  seed     copy sample files into the sandbox Inbox
  run      process-all on whatever is currently in the Inbox
  reset    wipe sandbox/workspace AND recreate an empty layout (Inbox ready)
  tree     show the inbox / queue / library state
  log      tail the actions log

Any other ProcraFiler command is passed straight through, e.g.:
  ./run.sh search bateau · ./run.sh language fr · ./run.sh enrich-keywords
  ./run.sh reindex · ./run.sh deletion-mode · ./run.sh status · ./run.sh doctor
  ./run.sh review · ./run.sh rescan · ./run.sh deleted-history
USAGE
    ;;
  reset)  confirm_wipe
          wipe_sandbox
          procra init-layout >/dev/null
          echo "Sandbox reset — empty layout recreated. Drop files into: $WS/Inbox" ;;
  init)   procra init-layout ;;
  seed)   seed ;;
  run)    procra process-all ;;
  setup-context) PROCRAFILER_CONTEXT_FILE="$REPO/context.txt" procra setup-context ;;
  tree)   tree_view ;;
  log)    tail -n 40 "$PROCRAFILER_HOME/actions_log.jsonl" 2>/dev/null || echo "(no log yet)" ;;
  e2e)
          echo "### reset";   confirm_wipe; wipe_sandbox
          echo "### init";    procra init-layout >/dev/null && echo "ok"
          echo "### doctor";  procra doctor || true
          echo "### seed";    seed
          echo "### process-all"; procra process-all
          echo "### result";  tree_view
          ;;
  # Any other ProcraFiler subcommand (search, language, enrich-keywords, reindex,
  # deletion-mode, status, doctor, review, rescan, deleted-history…) passes through.
  *)      procra "$@" ;;
esac
