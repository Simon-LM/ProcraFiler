#!/usr/bin/env bash
# Move an installation to the latest published release.
#
# Everything happens inside the installation's OWN source clone ($APP_DIR/src).
# The clone the user installed from is never fetched into, never checked out, and
# need not exist any more.
#
# That was the defect: `update.sh` used to read REPO_ROOT — the path of whatever
# directory install.sh had been run from — and `git checkout <tag>` inside it. Run
# by anyone who installed from a working tree, it moved their HEAD onto a release
# tag and left them detached. Run by anyone who had since tidied that folder away,
# it refused and left them with no way to update at all.
#
# An installation made by that older installer is REPAIRED on first run here, not
# rejected: its source is copied out of the old clone once, and the old clone is
# never touched again.
set -euo pipefail

MODE="user"
PREFIX="/usr/local"
SKIP_GIT_PULL="false"

usage() {
  cat <<'EOF'
Usage: ./scripts/update.sh [options]

Options:
  --mode <user|system>    Installation mode (default: user)
  --prefix <path>         Install prefix for system mode (default: /usr/local)
  --ref <tag|commit>      Update to this revision instead of the latest release tag
  --skip-git-pull         Reinstall the current revision without fetching
  -h, --help              Show help
EOF
}

REF=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --skip-git-pull) SKIP_GIT_PULL="true"; shift ;;
    # Accepted and ignored: the venv already exists, and its interpreter is the
    # one that must keep serving it. Silently re-creating it with another Python
    # is not an update, it is a reinstall.
    --python) shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
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

# Parse install-meta.env without `source` to avoid arbitrary shell execution
# if the metadata file is ever tampered with. Only known keys are read.
read_meta() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" "$META_FILE" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    return 1
  fi
  printf '%s' "${line#"${key}"=}"
}

VENV_DIR="$(read_meta VENV_DIR)" || { echo "Missing VENV_DIR in $META_FILE" >&2; exit 1; }
SRC_DIR="$(read_meta SRC_DIR || true)"
SOURCE_KIND="$(read_meta SOURCE_KIND || echo unknown)"

# --- Installations made by the older installer ------------------------------
#
# They recorded only REPO_ROOT: the user's own clone, which this script used to
# check a tag out inside. Using it again would re-inflict exactly the defect this
# rewrite removes, on the very people it was inflicted on. So don't use it — copy
# out of it, once, and never touch it again. Only when that clone is gone is there
# nothing to work from, and only then do we send the user back to install.sh.
if [[ -z "$SRC_DIR" ]]; then
  OLD_REPO="$(read_meta REPO_ROOT || true)"
  if [[ -z "$OLD_REPO" || ! -d "$OLD_REPO/.git" ]]; then
    echo "This installation predates the self-contained installer: it was built from" >&2
    echo "a clone of your own, and that clone is no longer there:" >&2
    echo "  ${OLD_REPO:-<not recorded>}" >&2
    echo >&2
    echo "There is nothing left to copy the source from. Reinstall once — after that," >&2
    echo "the installation owns its source and updates need no clone of yours:" >&2
    echo "  ./scripts/install.sh --mode $MODE --reinstall" >&2
    exit 1
  fi

  echo "This installation predates the self-contained installer: it was built from"
  echo "your clone at $OLD_REPO, and updating used to check a release tag out INSIDE"
  echo "it. Giving the installation its own source copy instead — your clone is read"
  echo "once here and never written to."
  SRC_DIR="$APP_DIR/src"
  rm -rf "$SRC_DIR"
  # --no-hardlinks: the clone and the installation are routinely on different
  # filesystems, where git's default local optimisation fails outright.
  git clone --no-hardlinks --quiet "$OLD_REPO" "$SRC_DIR"
  UPSTREAM="$(git -C "$OLD_REPO" remote get-url origin 2>/dev/null || true)"
  if [[ -n "$UPSTREAM" ]]; then
    git -C "$SRC_DIR" remote set-url origin "$UPSTREAM"
  fi
  {
    echo "SRC_DIR=$SRC_DIR"
    echo "SOURCE_KIND=git"
    echo "SOURCE_URL=${UPSTREAM:-$OLD_REPO}"
  } >> "$META_FILE"
  chmod 600 "$META_FILE"
  SOURCE_KIND="git"
  echo "Repaired: the source now lives at $SRC_DIR. You may delete $OLD_REPO."
fi

if [[ "$SOURCE_KIND" == "directory" ]]; then
  echo "This installation was made from a plain directory, not a git clone." >&2
  echo "There is nothing to update from. Reinstall from a clone:" >&2
  echo "  ./scripts/install.sh --mode $MODE --reinstall" >&2
  exit 1
fi

if [[ ! -d "$SRC_DIR/.git" ]]; then
  echo "The installation's source clone is missing or damaged: $SRC_DIR" >&2
  echo "Reinstall it: ./scripts/install.sh --mode $MODE --reinstall" >&2
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/pip" ]]; then
  echo "pip not found in venv: $VENV_DIR/bin/pip" >&2
  exit 1
fi

# Nobody is supposed to edit the installation's own source tree, so finding it
# modified means something has gone wrong — a half-finished previous update, or an
# edit made in the wrong directory. Checking a tag out over it would either fail or
# silently carry those changes into the installed package.
if [[ -n "$(git -C "$SRC_DIR" status --porcelain)" ]]; then
  echo "Refusing to update: the installation's source tree has local changes." >&2
  echo "  $SRC_DIR" >&2
  echo "Nothing should modify it. Reinstall to get a clean one:" >&2
  echo "  ./scripts/install.sh --mode $MODE --reinstall" >&2
  exit 1
fi

before="$(git -C "$SRC_DIR" describe --tags --always 2>/dev/null || echo unknown)"

if [[ "$SKIP_GIT_PULL" != "true" ]]; then
  # Always a RELEASE TAG, never a branch head, so an installation always tracks a
  # published version — the package version is derived from that tag by
  # setuptools-scm.
  git -C "$SRC_DIR" fetch --tags --prune --quiet
  target="$REF"
  if [[ -z "$target" ]]; then
    target="$(git -C "$SRC_DIR" tag --list 'v*' --sort=-v:refname | head -n 1)"
  fi
  if [[ -z "$target" ]]; then
    echo "No release tag (vX.Y.Z) found — nothing to update to." >&2
    exit 1
  fi
  if [[ "$before" == "$target" ]]; then
    echo "Already on the latest release: $target"
  else
    git -C "$SRC_DIR" checkout --quiet "$target"
    echo "Updating: $before -> $target"
  fi
fi

"$VENV_DIR/bin/pip" install --upgrade "$SRC_DIR"

# Keep the record true: a stale VERSION is worse than none, because install.sh and
# uninstall.sh report it as fact.
VERSION="$("$VENV_DIR/bin/procrafiler" --version 2>/dev/null | awk '{print $NF}' || echo unknown)"
COMMIT="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
SOURCE_REF="$(git -C "$SRC_DIR" describe --tags --always 2>/dev/null || echo unknown)"

tmp_meta="$(mktemp)"
grep -vE '^(VERSION|COMMIT|SOURCE_REF)=' "$META_FILE" > "$tmp_meta" || true
{
  echo "VERSION=$VERSION"
  echo "COMMIT=$COMMIT"
  echo "SOURCE_REF=$SOURCE_REF"
} >> "$tmp_meta"
cat "$tmp_meta" > "$META_FILE"
rm -f "$tmp_meta"
chmod 600 "$META_FILE"

echo "ProcraFiler is now at version: $VERSION   Revision: $SOURCE_REF"
