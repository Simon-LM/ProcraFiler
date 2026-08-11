#!/usr/bin/env bash
# Install ProcraFiler.
#
# The installation OWNS ITS SOURCE. The clone you run this from is read once, as a
# git source, and never written to again — not by this script, not by update.sh.
#
# That is a deliberate change from the earlier design, which recorded the path of
# your clone and later ran `git checkout <tag>` inside it. Installing from a
# development tree is legitimate and documented (docs/dev-prod-isolation.md); what
# followed from it was not, because an update would then move a developer's HEAD
# onto a release tag and leave them detached. And a clone is an ordinary thing to
# delete after installing, which used to leave the app with no way to update and no
# way to uninstall.
#
# So: clone into $APP_DIR/src, check the latest release tag out THERE, install from
# it, and copy the uninstaller in beside it. What is installed then depends on
# nothing outside its own directory.
set -euo pipefail

MODE="user"
PREFIX="/usr/local"
PYTHON_BIN="python3"
REINSTALL="false"
REF=""

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [options]

Options:
  --mode <user|system>    Installation mode (default: user)
  --prefix <path>         Install prefix for system mode (default: /usr/local)
  --python <binary>       Python executable (default: python3)
  --ref <tag|commit>      Install this revision instead of the latest release tag
  --reinstall             Replace an existing installation (refused by default)
  -h, --help              Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --reinstall) REINSTALL="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
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
SRC_DIR="$APP_DIR/src"
META_FILE="$APP_DIR/install-meta.env"
ENV_FILE="$ENV_DIR/procrafiler.env"

read_meta() {
  # Parsed rather than sourced: this file must never be able to execute anything.
  local key="$1" line
  line="$(grep -E "^${key}=" "$META_FILE" 2>/dev/null | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  printf '%s' "${line#"${key}"=}"
}

# --- One installation, and no accidental second one -------------------------
#
# Silently reinstalling over a live installation is how a working setup acquires a
# half-replaced venv and a source tree from a different version. Say what is there,
# and make replacing it a decision.
if [[ -f "$META_FILE" && "$REINSTALL" != "true" ]]; then
  echo "ProcraFiler is already installed here." >&2
  echo "  app:      $APP_DIR" >&2
  echo "  version:  $(read_meta VERSION || echo unknown)" >&2
  echo "  revision: $(read_meta SOURCE_REF || echo unknown)" >&2
  echo "  from:     $(read_meta SOURCE_URL || read_meta REPO_ROOT || echo unknown)" >&2
  echo >&2
  echo "To move it to another version, update it in place:" >&2
  echo "  $SCRIPT_DIR/update.sh --mode $MODE" >&2
  echo "To replace this installation from scratch:" >&2
  echo "  $SCRIPT_DIR/install.sh --mode $MODE --reinstall" >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$BIN_DIR" "$ENV_DIR"

# --- Acquire the source, into the installation's own directory ---------------
SOURCE_KIND="directory"
SOURCE_URL=""
SOURCE_REF=""
BUILD_DIR="$REPO_ROOT"

if [[ -d "$REPO_ROOT/.git" ]] && command -v git >/dev/null 2>&1; then
  SOURCE_KIND="git"
  rm -rf "$SRC_DIR"
  # --no-hardlinks because the clone and the installation are routinely on
  # different filesystems (a checkout on an external disk, a home on the system
  # one), and hardlinks cannot cross a device boundary — git fails outright.
  git clone --no-hardlinks --quiet "$REPO_ROOT" "$SRC_DIR"

  # Point at the real upstream when the source clone has one, so updates keep
  # working after the user deletes the clone they installed from.
  UPSTREAM="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  if [[ -n "$UPSTREAM" ]]; then
    git -C "$SRC_DIR" remote set-url origin "$UPSTREAM"
    SOURCE_URL="$UPSTREAM"
  else
    SOURCE_URL="$REPO_ROOT"
  fi

  # A release tag, not a branch head: the version is derived from the tag by
  # setuptools-scm, so an install always names a published version.
  if [[ -z "$REF" ]]; then
    REF="$(git -C "$SRC_DIR" tag --list 'v*' --sort=-v:refname | head -n 1)"
  fi
  if [[ -n "$REF" ]]; then
    git -C "$SRC_DIR" checkout --quiet "$REF"
  else
    echo "Note: no release tag found; installing the default branch as-is." >&2
  fi
  SOURCE_REF="$(git -C "$SRC_DIR" describe --tags --always 2>/dev/null || echo unknown)"
  BUILD_DIR="$SRC_DIR"
else
  echo "Note: $REPO_ROOT is not a git clone — installing it as a plain directory." >&2
  echo "      update.sh will have nothing to update from." >&2
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel
"$VENV_DIR/bin/pip" install --upgrade "$BUILD_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  # Seed the env file from the canonical template (.env.example) so it can never
  # drift from the AI tasks the app actually reads. The file holds API keys, so
  # create it 0600 FIRST (umask 077), then fill it — never world-readable, even
  # momentarily. An existing env file is left untouched.
  (
    umask 077
    : > "$ENV_FILE"
    if [[ -f "$BUILD_DIR/.env.example" ]]; then
      cat "$BUILD_DIR/.env.example" > "$ENV_FILE"
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

# --- The uninstaller travels with the installation ---------------------------
#
# It used to live only in the clone. Delete the clone — an ordinary thing to do
# after installing — and there was no way left to remove the app, and nothing
# telling you which directories it had created.
cp "$BUILD_DIR/scripts/uninstall.sh" "$APP_DIR/uninstall.sh"
chmod +x "$APP_DIR/uninstall.sh"

cat > "$BIN_DIR/procrafiler-uninstall" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/uninstall.sh" --mode "$MODE" --prefix "$PREFIX" "\$@"
EOF
chmod +x "$BIN_DIR/procrafiler-uninstall"

VERSION="$("$VENV_DIR/bin/procrafiler" --version 2>/dev/null | awk '{print $NF}' || echo unknown)"
COMMIT="unknown"
if [[ "$SOURCE_KIND" == "git" ]]; then
  COMMIT="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
fi

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
SRC_DIR=$SRC_DIR
SOURCE_KIND=$SOURCE_KIND
SOURCE_URL=$SOURCE_URL
SOURCE_REF=$SOURCE_REF
VERSION=$VERSION
COMMIT=$COMMIT
EOF
)
chmod 600 "$META_FILE"

echo "ProcraFiler installed successfully."
echo "Version: $VERSION   Revision: ${SOURCE_REF:-unknown}"
echo "Binary: $BIN_DIR/procrafiler"
echo "Uninstall: $BIN_DIR/procrafiler-uninstall"
if [[ "$MODE" == "user" ]]; then
  echo "If needed, add this to your shell profile: export PATH=\"$HOME/.local/bin:\$PATH\""
fi
echo
echo "Next step — run the guided first-time setup:"
echo "  procrafiler setup        # choose where your files live (Inbox/Library/optional Mirror), then who you are"
echo "Then set your AI key in: $ENV_FILE   (MISTRAL_API_KEY=…, or point a task at a local Ollama)"
