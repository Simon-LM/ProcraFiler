#!/usr/bin/env bash
# Remove ProcraFiler. Your organized library is NEVER touched.
#
# Three things this script used to get wrong, each of which mattered:
#
# 1. It printed "✓ Removed the ProcraFiler app" unconditionally, after `rm -f` and
#    `rm -rf` that stay silent on a missing target. Install with --mode system,
#    uninstall without options (the default is user), and it deleted two paths that
#    were never there, declared victory, and left the real installation in place.
#    Every removal is now reported per target: removed / already absent.
#
# 2. Its purge list was written in bash while the truth lives in `config.py`, and
#    the two drifted. It named a `search_index.db` a given layout never had and
#    missed the runtime lock, the state directory itself and the stale
#    subdirectories of older versions — so "purged" left a tree behind. It now ASKS
#    the installed app (`procrafiler paths`) instead of restating it.
#
# 3. --purge honoured PROCRAFILER_HOME / PROCRAFILER_CONFIG_HOME from the
#    environment. Run in a shell where those were exported — which this project's
#    own sandbox/run.sh does — it purged whatever they pointed at, not the
#    installation. It now refuses rather than guess which one you meant.
#
# 4. --purge kept the user's context file, on the grounds that it is theirs. But it
#    holds who they are, what they do for a living and the names that matter to
#    them, and it stayed behind in ~/.config on a machine they had just wiped the
#    app from. It is now removed like the rest — after the user has been OFFERED a
#    copy and told where it went. Offered, not imposed: writing a copy of somebody's
#    personal notes somewhere they did not ask for is the same leak under a new name.
set -euo pipefail

# The uninstaller is installed INSIDE the directory it deletes. Deleting a script
# while bash is still reading it is undefined; re-exec from a copy first.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
if [[ "${PROCRAFILER_UNINSTALL_DETACHED:-}" != "1" ]]; then
  DETACHED="$(mktemp)"
  cat "$SELF" > "$DETACHED"
  chmod +x "$DETACHED"
  PROCRAFILER_UNINSTALL_DETACHED=1 "$DETACHED" "$@"
  status=$?
  rm -f "$DETACHED"
  exit $status
fi

MODE="user"
PREFIX="/usr/local"
PURGE="false"
ASSUME_YES="false"
KEEP_CONTEXT="ask"

usage() {
  cat <<'EOF'
Usage: ./scripts/uninstall.sh [options]

Removes the ProcraFiler app (launcher + venv + source + code). Your organized
library is NEVER touched by this script.

Options:
  --mode <user|system>    Installation mode (default: user)
  --prefix <path>         Install prefix for system mode (default: /usr/local)
  --purge                 Also remove the app config + regenerable state (env
                          file, settings, policy, catalog, logs, search index)
                          and your context file — but NEVER your library.
                          You are asked first whether to keep a copy of the
                          context file, and told where the copy was written.
  --keep-context          Answer that question up front: yes, copy it out.
  --drop-context          Answer it up front: no copy, remove it.
  --yes                   Do not prompt for confirmation on --purge. Without
                          --keep-context this means NO copy of your context
                          file is kept: unattended runs must not scatter your
                          personal notes across the disk.
  -h, --help              Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --purge) PURGE="true"; shift ;;
    --keep-context) KEEP_CONTEXT="yes"; shift ;;
    --drop-context) KEEP_CONTEXT="no"; shift ;;
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
  APP_DIR="/opt/procrafiler/app"
  BIN_DIR="$PREFIX/bin"
else
  APP_DIR="$HOME_DIR/.local/share/procrafiler/app"
  BIN_DIR="$HOME_DIR/.local/bin"
fi
META_FILE="$APP_DIR/install-meta.env"

read_meta() {
  # Parsed rather than sourced: this file must never be able to execute anything.
  local key="$1" line
  line="$(grep -E "^${key}=" "$META_FILE" 2>/dev/null | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  printf '%s' "${line#"${key}"=}"
}

# --- Refuse to guess which installation is meant -----------------------------
ROOT_VARS=(PROCRAFILER_WORKSPACE_DIR PROCRAFILER_LIBRARY_DIR
           PROCRAFILER_LIBRARY_MIRROR_DIR PROCRAFILER_HOME PROCRAFILER_CONFIG_HOME)
if [[ "$PURGE" == "true" ]]; then
  exported=()
  for name in "${ROOT_VARS[@]}"; do
    [[ -n "${!name:-}" ]] && exported+=("$name=${!name}")
  done
  if [[ ${#exported[@]} -gt 0 ]]; then
    echo "Refusing to purge: these are set in this shell and would decide what gets deleted." >&2
    printf '  %s\n' "${exported[@]}" >&2
    echo >&2
    echo "They redirect the catalog and config elsewhere — a development sandbox, for" >&2
    echo "instance. Purging would erase THAT, not the installation. Start a clean shell." >&2
    exit 1
  fi
fi

# --- Ask the app where its files are, rather than restating config.py ---------
LAUNCHER="$BIN_DIR/procrafiler"
VENV_DIR="$(read_meta VENV_DIR || true)"
PATHS_JSON=""
if [[ -n "$VENV_DIR" && -x "$VENV_DIR/bin/procrafiler" ]]; then
  ENV_FILE_META="$(read_meta ENV_FILE || true)"
  PATHS_JSON="$(PROCRAFILER_ENV_FILE="${ENV_FILE_META:-/dev/null}" \
                "$VENV_DIR/bin/procrafiler" paths 2>/dev/null || true)"
fi

# Fall back to the documented defaults when the app cannot answer — a half-removed
# or older installation must still be cleanable.
CONFIG_DIR="$(read_meta ENV_FILE 2>/dev/null | xargs -r dirname || true)"
CONFIG_DIR="${CONFIG_DIR:-$HOME_DIR/.config/procrafiler}"
STATE_DIR="$HOME_DIR/.local/share/procrafiler"
LIBRARY_DIR="$HOME_DIR/ProcraFiler_Library"

PURGE_FILES=()
PURGE_DIRS=()
PERSONAL_FILES=()
if [[ -n "$PATHS_JSON" ]]; then
  # The venv's own python — no jq dependency, and it is the interpreter that
  # produced the JSON in the first place.
  mapfile -t PURGE_FILES < <(printf '%s' "$PATHS_JSON" | "$VENV_DIR/bin/python" -c \
    'import json,sys; [print(p) for p in json.load(sys.stdin)["purge_files"]]' 2>/dev/null || true)
  mapfile -t PURGE_DIRS < <(printf '%s' "$PATHS_JSON" | "$VENV_DIR/bin/python" -c \
    'import json,sys; [print(p) for p in json.load(sys.stdin)["purge_dirs"]]' 2>/dev/null || true)
  mapfile -t PERSONAL_FILES < <(printf '%s' "$PATHS_JSON" | "$VENV_DIR/bin/python" -c \
    'import json,sys; [print(p) for p in json.load(sys.stdin).get("personal_files", [])]' 2>/dev/null || true)
  LIBRARY_DIR="$(printf '%s' "$PATHS_JSON" | "$VENV_DIR/bin/python" -c \
    'import json,sys; print(json.load(sys.stdin)["paths"]["library_root"])' 2>/dev/null || echo "$LIBRARY_DIR")"
fi
if [[ ${#PURGE_FILES[@]} -eq 0 ]]; then
  PURGE_FILES=(
    "$CONFIG_DIR/settings.json" "$CONFIG_DIR/policy.toml"
    "$STATE_DIR/catalog.db" "$STATE_DIR/catalog_snapshot.json"
    "$STATE_DIR/search_index.db" "$STATE_DIR/actions_log.jsonl"
  )
  PURGE_DIRS=("$STATE_DIR")
fi
# An app too old to report them still had them, under these names.
if [[ ${#PERSONAL_FILES[@]} -eq 0 ]]; then
  PERSONAL_FILES=("$CONFIG_DIR/context.txt" "$CONFIG_DIR/context.md")
fi
# The env file is the installation's, not the layout's, so it comes from the
# metadata rather than from `paths`.
ENV_FILE="$(read_meta ENV_FILE || echo "$CONFIG_DIR/procrafiler.env")"

removed_any="false"
remove_path() {
  local target="$1" label="$2"
  if [[ -e "$target" || -L "$target" ]]; then
    rm -rf "$target"
    echo "  removed        $label: $target"
    removed_any="true"
  else
    echo "  already absent $label: $target"
  fi
}

# --- 1) The app itself: launchers, venv, source, metadata --------------------
echo "Removing the ProcraFiler app (mode: $MODE):"
remove_path "$LAUNCHER" "launcher"
remove_path "$BIN_DIR/procrafiler-uninstall" "uninstall launcher"
remove_path "$APP_DIR" "app"

if [[ "$removed_any" != "true" ]]; then
  echo
  echo "Nothing was found to remove for --mode $MODE." >&2
  echo "If you installed the other way round, try:" >&2
  if [[ "$MODE" == "user" ]]; then
    echo "  sudo $0 --mode system" >&2
  else
    echo "  $0 --mode user" >&2
  fi
  exit 1
fi

# --- 2) Optional purge of config + regenerable state -------------------------
CONTEXT_COPIES=()
copy_out() {
  # Never overwrite, never widen: this file says who its author is.
  local src="$1" base ext dest stamp n=1
  base="$(basename "$src")"
  ext=""
  if [[ "$base" == *.* ]]; then ext=".${base##*.}"; fi
  stamp="$(date +%Y%m%d-%H%M%S)"
  dest="$HOME_DIR/procrafiler-context-$stamp$ext"
  while [[ -e "$dest" ]]; do
    dest="$HOME_DIR/procrafiler-context-$stamp-$n$ext"
    n=$((n + 1))
  done
  if cp "$src" "$dest" 2>/dev/null; then
    chmod 600 "$dest" 2>/dev/null || true
    CONTEXT_COPIES+=("$dest")
    echo "  copied out     context file -> $dest"
    return 0
  fi
  return 1
}

if [[ "$PURGE" == "true" ]]; then
  targets=("$ENV_FILE" "${PURGE_FILES[@]}" "${PURGE_DIRS[@]}")
  present=()
  for t in "${targets[@]}"; do
    [[ -n "$t" && -e "$t" ]] && present+=("$t")
  done
  personal=()
  for t in "${PERSONAL_FILES[@]}"; do
    [[ -n "$t" && -e "$t" ]] && personal+=("$t")
  done

  if [[ ${#present[@]} -eq 0 && ${#personal[@]} -eq 0 ]]; then
    echo
    echo "Nothing to purge (no config/state found)."
  else
    echo
    echo "--purge will remove these (config + regenerable state):"
    if [[ ${#present[@]} -gt 0 ]]; then
      printf '  - %s\n' "${present[@]}"
    fi
    if [[ ${#personal[@]} -gt 0 ]]; then
      echo "and your context file — what you wrote about yourself for the AI to read:"
      printf '  - %s\n' "${personal[@]}"
    fi
    echo "It will NOT remove your library ($LIBRARY_DIR), its mirror or the trashes."
    if [[ "$ASSUME_YES" != "true" ]]; then
      echo "Proceed with purge? [y/N]"
      read -r reply
      if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
        echo "Purge cancelled. The app is removed; your config and data are kept."
        exit 0
      fi
    fi

    # Asked only once the purge itself is agreed to, and only if there is one.
    if [[ ${#personal[@]} -gt 0 && "$KEEP_CONTEXT" == "ask" ]]; then
      if [[ "$ASSUME_YES" == "true" ]]; then
        # Unattended, so nobody can answer — and an unrequested copy of somebody's
        # personal notes is exactly what this purge is meant to stop leaving behind.
        # --keep-context is how you ask for one without a prompt.
        KEEP_CONTEXT="no"
      else
        echo
        echo "That context file is personal: who you are, your work, your household."
        # Asked with `echo`, not `read -p`: bash suppresses a read prompt whenever
        # stdin is not a terminal, and a question nobody sees is not a choice.
        echo "Keep a copy of it outside ProcraFiler before removing it? [y/N]"
        read -r reply
        if [[ "$reply" == "y" || "$reply" == "Y" ]]; then KEEP_CONTEXT="yes"; else KEEP_CONTEXT="no"; fi
      fi
    fi

    echo "Purging:"
    for t in "${present[@]}"; do
      remove_path "$t" "state/config"
    done
    for t in "${personal[@]}"; do
      # A failed copy must not become a silent deletion of the only original.
      if [[ "$KEEP_CONTEXT" == "yes" ]] && ! copy_out "$t"; then
        echo "  KEPT           context file: $t (could not write the copy — nothing removed)" >&2
        continue
      fi
      remove_path "$t" "context file"
    done
    # Only if it ended up empty — anything else in there is not ours to delete.
    rmdir "$CONFIG_DIR" 2>/dev/null && echo "  removed        empty config dir: $CONFIG_DIR" || true
  fi
fi

# --- 3) Make crystal clear what is preserved ---------------------------------
echo
echo "Your documents are safe — ProcraFiler never deletes your organized files."
echo "Kept:"
echo "  - Library:  $LIBRARY_DIR"
if [[ "$PURGE" != "true" ]]; then
  echo "  - State:    $STATE_DIR (catalog, logs, search index)"
  echo "  - Config:   $CONFIG_DIR (settings, policy, context, env with your API key)"
  echo "To also remove the config + state (never the library), re-run with --purge."
elif [[ ${#CONTEXT_COPIES[@]} -gt 0 ]]; then
  echo "Your context file was removed from ProcraFiler. The copy you asked for is at:"
  printf '  - %s\n' "${CONTEXT_COPIES[@]}"
fi
echo "ProcraFiler uninstalled (mode: $MODE)."
