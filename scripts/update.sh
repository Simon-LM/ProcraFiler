#!/usr/bin/env bash
set -euo pipefail

MODE="user"
PREFIX="/usr/local"
PYTHON_BIN="python3"

usage() {
  cat <<'EOF'
Usage: ./scripts/update.sh [options]

Options:
  --mode <user|system>    Installation mode (default: user)
  --prefix <path>         Install prefix for system mode (default: /usr/local)
  --python <binary>       Python executable (default: python3)
  --skip-git-pull         Skip git pull before reinstall
  -h, --help              Show help
EOF
}

SKIP_GIT_PULL="false"
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
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --skip-git-pull)
      SKIP_GIT_PULL="true"
      shift
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
else
  APP_DIR="$HOME/.local/share/procrafiler/app"
fi

META_FILE="$APP_DIR/install-meta.env"
if [[ ! -f "$META_FILE" ]]; then
  echo "Install metadata not found: $META_FILE" >&2
  echo "Run install.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$META_FILE"

if [[ ! -d "$REPO_ROOT" ]]; then
  echo "Repository path from metadata does not exist: $REPO_ROOT" >&2
  exit 1
fi

if [[ "$SKIP_GIT_PULL" != "true" ]] && [[ -d "$REPO_ROOT/.git" ]]; then
  git -C "$REPO_ROOT" fetch --tags --prune
  git -C "$REPO_ROOT" pull --ff-only
fi

"$VENV_DIR/bin/pip" install --upgrade "$REPO_ROOT"

echo "ProcraFiler updated successfully from: $REPO_ROOT"
