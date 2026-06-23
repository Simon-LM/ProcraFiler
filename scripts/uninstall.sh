#!/usr/bin/env bash
set -euo pipefail

MODE="user"
PREFIX="/usr/local"
PURGE="false"
ASSUME_YES="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/uninstall.sh [options]

Removes the ProcraFiler app (launcher + venv + code). Your organized library is
NEVER touched by this script.

Options:
  --mode <user|system>    Installation mode (default: user)
  --prefix <path>         Install prefix for system mode (default: /usr/local)
  --purge                 Also remove the app config + regenerable state
                          (env file, settings, policy, catalog, logs, search
                          index) — but NEVER your library or context file.
  --yes                   Do not prompt for confirmation on --purge
  -h, --help              Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --purge) PURGE="true"; shift ;;
    --yes) ASSUME_YES="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ "$MODE" != "user" && "$MODE" != "system" ]]; then
  echo "Invalid --mode value: $MODE" >&2
  exit 1
fi

HOME_DIR="${HOME:-/root}"

if [[ "$MODE" == "system" ]]; then
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "System mode requires root privileges. Run with sudo." >&2
    exit 1
  fi
  APP_DIR="/opt/procrafiler/app"
  BIN_DIR="$PREFIX/bin"
  ENV_FILE="/etc/procrafiler/procrafiler.env"
else
  APP_DIR="$HOME_DIR/.local/share/procrafiler/app"
  BIN_DIR="$HOME_DIR/.local/bin"
  ENV_FILE="$HOME_DIR/.config/procrafiler/procrafiler.env"
fi

# Where the user's data lives (honour env overrides, else the documented defaults).
STATE_DIR="${PROCRAFILER_HOME:-$HOME_DIR/.local/share/procrafiler}"
CONFIG_DIR="${PROCRAFILER_CONFIG_HOME:-$HOME_DIR/.config/procrafiler}"
LIBRARY_DIR="${PROCRAFILER_LIBRARY_DIR:-$HOME_DIR/ProcraFiler_Library}"

# 1) Remove the app itself (code + venv + launcher). Never any user data.
rm -f "$BIN_DIR/procrafiler"
rm -rf "$APP_DIR"
echo "✓ Removed the ProcraFiler app (launcher + venv + code)."

# 2) Optional purge of the app's config + regenerable state. NEVER the library.
if [[ "$PURGE" == "true" ]]; then
  targets=("$ENV_FILE")
  if [[ "$MODE" == "user" ]]; then
    targets+=(
      "$CONFIG_DIR/settings.json"
      "$CONFIG_DIR/policy.toml"
      "$STATE_DIR/catalog.db"
      "$STATE_DIR/catalog_snapshot.json"
      "$STATE_DIR/search_index.db"
      "$STATE_DIR/actions_log.jsonl"
    )
  fi
  present=()
  for t in "${targets[@]}"; do [[ -e "$t" ]] && present+=("$t"); done

  if [[ ${#present[@]} -eq 0 ]]; then
    echo "Nothing to purge (no config/state found)."
  else
    echo
    echo "--purge will remove these (config + regenerable state):"
    printf '  - %s\n' "${present[@]}"
    echo "It will NOT remove your library ($LIBRARY_DIR), its mirror, the trashes, or your context file."
    if [[ "$MODE" == "system" ]]; then
      echo "Note: per-user state/config live in each user's home and are NOT removed by a system uninstall."
    fi
    if [[ "$ASSUME_YES" != "true" ]]; then
      read -r -p "Proceed with purge? [y/N] " reply
      if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
        echo "Purge cancelled. The app is removed; your config and data are kept."
        exit 0
      fi
    fi
    for t in "${present[@]}"; do rm -f "$t"; done
    rmdir "$CONFIG_DIR" 2>/dev/null || true  # only if now empty
    echo "✓ Purged config + state."
  fi
fi

# 3) Make crystal clear what is preserved.
echo
echo "Your documents are safe — ProcraFiler never deletes your organized files."
echo "Kept:"
echo "  - Library:  $LIBRARY_DIR"
if [[ "$PURGE" != "true" ]]; then
  if [[ "$MODE" == "user" ]]; then
    echo "  - State:    $STATE_DIR (catalog, logs, search index)"
    echo "  - Config:   $CONFIG_DIR (settings, policy, context, env with your API key)"
  else
    echo "  - Each user's state/config in their own home"
  fi
  echo "To also remove the config + state (never the library), re-run with --purge."
fi
echo "ProcraFiler uninstalled (mode: $MODE)."
