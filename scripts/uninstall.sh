#!/usr/bin/env bash
set -euo pipefail

MODE="user"
PREFIX="/usr/local"

usage() {
  cat <<'EOF'
Usage: ./scripts/uninstall.sh [options]

Options:
  --mode <user|system>    Installation mode (default: user)
  --prefix <path>         Install prefix for system mode (default: /usr/local)
  -h, --help              Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
    --prefix)
      PREFIX="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$MODE" != "user" && "$MODE" != "system" ]]; then
  echo "Invalid --mode value: $MODE" >&2
  exit 1
fi

if [[ "$MODE" == "system" ]]; then
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "System mode requires root privileges. Run with sudo." >&2
    exit 1
  fi
  APP_DIR="/opt/procrafiler/app"
  BIN_DIR="$PREFIX/bin"
else
  APP_DIR="$HOME/.local/share/procrafiler/app"
  BIN_DIR="$HOME/.local/bin"
fi

rm -f "$BIN_DIR/procrafiler"
rm -rf "$APP_DIR"

echo "ProcraFiler uninstalled from mode: $MODE"
