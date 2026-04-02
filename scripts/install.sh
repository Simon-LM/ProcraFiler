#!/usr/bin/env bash
set -euo pipefail

MODE="user"
PREFIX="/usr/local"
PYTHON_BIN="python3"

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [options]

Options:
  --mode <user|system>    Installation mode (default: user)
  --prefix <path>         Install prefix for system mode (default: /usr/local)
  --python <binary>       Python executable (default: python3)
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
    --python)
      PYTHON_BIN="$2"
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

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$MODE" == "system" ]]; then
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "System mode requires root privileges. Run with sudo." >&2
    exit 1
  fi
  APP_DIR="/opt/procrafiler/app"
  BIN_DIR="$PREFIX/bin"
  ENV_DIR="/etc/procrafiler"
else
  APP_DIR="$HOME/.local/share/procrafiler/app"
  BIN_DIR="$HOME/.local/bin"
  ENV_DIR="$HOME/.config/procrafiler"
fi

VENV_DIR="$APP_DIR/.venv"
META_FILE="$APP_DIR/install-meta.env"
ENV_FILE="$ENV_DIR/procrafiler.env"

mkdir -p "$APP_DIR" "$BIN_DIR" "$ENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel
"$VENV_DIR/bin/pip" install --upgrade "$REPO_ROOT"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<'EOF'
# ProcraFiler runtime configuration
# Per-task AI selection (provider:model,provider:model)
# Set PRIMARY chain for each task, and optional FALLBACK chain.
PROCRAFILER_AI_NAMING_PRIMARY=
PROCRAFILER_AI_NAMING_FALLBACK=
PROCRAFILER_AI_OCR_PRIMARY=
PROCRAFILER_AI_OCR_FALLBACK=
PROCRAFILER_AI_PDF_PRIMARY=
PROCRAFILER_AI_PDF_FALLBACK=
PROCRAFILER_AI_IMAGE_PRIMARY=
PROCRAFILER_AI_IMAGE_FALLBACK=
PROCRAFILER_AI_VIDEO_PRIMARY=
PROCRAFILER_AI_VIDEO_FALLBACK=
PROCRAFILER_AI_SUPERVISOR_PRIMARY=
PROCRAFILER_AI_SUPERVISOR_FALLBACK=
PROCRAFILER_AI_CLASSIFICATION_PRIMARY=
PROCRAFILER_AI_CLASSIFICATION_FALLBACK=

# Retry/timeout defaults (can be overridden per task)
PROCRAFILER_AI_TIMEOUT=60
PROCRAFILER_AI_RETRIES=2
PROCRAFILER_AI_NAMING_TIMEOUT=60
PROCRAFILER_AI_NAMING_RETRIES=2

# Provider keys
MISTRAL_API_KEY=
EOF
fi

cat > "$BIN_DIR/procrafiler" <<EOF
#!/usr/bin/env bash
export PROCRAFILER_ENV_FILE="$ENV_FILE"
exec "$VENV_DIR/bin/procrafiler" "\$@"
EOF
chmod +x "$BIN_DIR/procrafiler"

cat > "$META_FILE" <<EOF
MODE=$MODE
PREFIX=$PREFIX
PYTHON_BIN=$PYTHON_BIN
REPO_ROOT=$REPO_ROOT
APP_DIR=$APP_DIR
BIN_DIR=$BIN_DIR
VENV_DIR=$VENV_DIR
ENV_FILE=$ENV_FILE
EOF

echo "ProcraFiler installed successfully."
echo "Binary: $BIN_DIR/procrafiler"
if [[ "$MODE" == "user" ]]; then
  echo "If needed, add this to your shell profile: export PATH=\"$HOME/.local/bin:\$PATH\""
fi
