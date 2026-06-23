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
  # Seed the env file from the canonical template (.env.example) so it can never
  # drift from the AI tasks the app actually reads. The file holds API keys, so
  # create it 0600 FIRST (umask 077), then fill it — never world-readable, even
  # momentarily. An existing env file is left untouched.
  (
    umask 077
    : > "$ENV_FILE"
    if [[ -f "$REPO_ROOT/.env.example" ]]; then
      cat "$REPO_ROOT/.env.example" > "$ENV_FILE"
    else
      echo "# .env.example not found at install time; fill in your AI chains + MISTRAL_API_KEY." > "$ENV_FILE"
    fi
  )
fi

# Always enforce restrictive permissions, even on pre-existing env files
# (e.g. created by a prior install version that did not set 0600).
if [[ "$MODE" == "system" ]]; then
  chmod 640 "$ENV_FILE"
else
  chmod 600 "$ENV_FILE"
fi

cat > "$BIN_DIR/procrafiler" <<EOF
#!/usr/bin/env bash
export PROCRAFILER_ENV_FILE="$ENV_FILE"
exec "$VENV_DIR/bin/procrafiler" "\$@"
EOF
chmod +x "$BIN_DIR/procrafiler"

(
  umask 077
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
)
chmod 600 "$META_FILE"

echo "ProcraFiler installed successfully."
echo "Binary: $BIN_DIR/procrafiler"
if [[ "$MODE" == "user" ]]; then
  echo "If needed, add this to your shell profile: export PATH=\"$HOME/.local/bin:\$PATH\""
fi
echo
echo "Next step — run the guided first-time setup:"
echo "  procrafiler setup        # choose where your files live (Inbox/Library/optional Mirror), then who you are"
echo "Then set your AI key in: $ENV_FILE   (MISTRAL_API_KEY=…, or point a task at a local Ollama)"
