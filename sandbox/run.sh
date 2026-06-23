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

WS="$PROCRAFILER_WORKSPACE_DIR"
LIB="$PROCRAFILER_LIBRARY_DIR"

procra() { "$PY" -m procrafiler.cli "$@"; }

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

case "${1:-help}" in
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
  reset)  rm -rf "$WS" "$LIB" "${LIB}_Trash_Manual" "$PROCRAFILER_LIBRARY_MIRROR_DIR" "$PROCRAFILER_HOME" "$PROCRAFILER_CONFIG_HOME"
          procra init-layout >/dev/null
          echo "Sandbox reset — empty layout recreated. Drop files into: $WS/Inbox" ;;
  init)   procra init-layout ;;
  seed)   seed ;;
  run)    procra process-all ;;
  setup-context) PROCRAFILER_CONTEXT_FILE="$REPO/context.txt" procra setup-context ;;
  tree)   tree_view ;;
  log)    tail -n 40 "$PROCRAFILER_HOME/actions_log.jsonl" 2>/dev/null || echo "(no log yet)" ;;
  e2e)
          echo "### reset";   rm -rf "$WS" "$LIB" "${LIB}_Trash_Manual" "$PROCRAFILER_LIBRARY_MIRROR_DIR" "$PROCRAFILER_HOME" "$PROCRAFILER_CONFIG_HOME"
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
