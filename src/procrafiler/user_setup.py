"""Interactive `setup` first-run: choose WHERE ProcraFiler keeps your files
(Inbox, Library, optional Mirror), persist those paths to the env file, create
ONLY the folders you chose, then flow straight into `setup-context` (WHO you
are) — a single guided first run.

Why a command and not prompts in install.sh: install stays non-interactive
(scriptable, CI-friendly), and this Python flow is fully testable offline via
injectable ask/out callables (no real stdin, no real home touched), exactly like
`user_context_setup`.

The mirror is OPTIONAL: a user who declines it gets no mirror folder and the
`mirror_sync` feature turned off (the pipeline then skips mirroring cleanly).
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Callable

from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    set_feature_flag,
)
from procrafiler.user_context_setup import setup_context

AskFn = Callable[[str], str]
OutFn = Callable[[str], None]
SecretFn = Callable[[str], str]  # like ask, but the input is not echoed (API key)

# The three locations the user chooses, mapped to the env vars the app reads.
_INBOX_ENV = "PROCRAFILER_WORKSPACE_DIR"
_LIBRARY_ENV = "PROCRAFILER_LIBRARY_DIR"
_MIRROR_ENV = "PROCRAFILER_LIBRARY_MIRROR_DIR"

# Tested provider presets written to the env file (see docs/ai-providers.md).
_MISTRAL_PRESET = {
    "PROCRAFILER_AI_ANALYSIS_PRIMARY": "mistral:mistral-small-latest",
    "PROCRAFILER_AI_ORGANIZE_PRIMARY": "mistral:mistral-medium-latest",
    "PROCRAFILER_AI_OCR_PRIMARY": "mistral:mistral-ocr-latest",
    "PROCRAFILER_AI_IMAGE_PRIMARY": "mistral:mistral-medium-latest",
}
_OLLAMA_PRESET = {
    "PROCRAFILER_AI_ANALYSIS_PRIMARY": "ollama:qwen3.5:9b",
    "PROCRAFILER_AI_ORGANIZE_PRIMARY": "ollama:gemma4:12b",
    "PROCRAFILER_AI_OCR_PRIMARY": "ollama:minicpm-v",
    "PROCRAFILER_AI_IMAGE_PRIMARY": "ollama:qwen2.5vl:7b",
    # No timeout here on purpose: local (ollama) calls get a generous default
    # automatically (see _task_timeout_from_env); override only to change it.
}


def default_setup_paths() -> dict[str, Path]:
    """The canonical home-based defaults proposed by `setup` (independent of any
    currently-loaded env, so the suggestion is always the clean default)."""
    home = Path.home()
    return {
        "inbox": home / "Downloads" / "ProcraFiler_Inbox",
        "library": home / "ProcraFiler_Library",
        "mirror": home / "ProcraFiler_Library_Mirror",
    }


def setup_env_path() -> Path:
    """The env file `setup` writes the chosen paths to: the explicitly configured
    one if set, else the user-install location under the config home."""
    explicit = os.environ.get("PROCRAFILER_ENV_FILE")
    if explicit:
        return Path(explicit)
    config_home = Path(
        os.environ.get("PROCRAFILER_CONFIG_HOME", str(Path.home() / ".config" / "procrafiler"))
    )
    return config_home / "procrafiler.env"


def update_env_file(
    env_path: Path, updates: dict[str, str], unset_keys: set[str] | None = None
) -> None:
    """Write `KEY=value` for each key in `updates` into the env file and drop any
    ACTIVE line for each key in `unset_keys`, while preserving every other line
    (the user's API key and AI chains) and the file order.

    A commented template line (`# KEY=…`) for an updated key is replaced in place
    by the real value; a commented line for an unset key is left untouched. A new
    file is created with 0600 permissions.
    """
    unset_keys = unset_keys or set()
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    is_new = not env_path.exists()

    out_lines: list[str] = []
    written: set[str] = set()
    for line in existing:
        stripped = line.strip()
        is_comment = stripped.startswith("#")
        bare = stripped[1:].strip() if is_comment else stripped
        key = bare.split("=", 1)[0].strip() if "=" in bare else None
        if key in updates:
            if key not in written:
                out_lines.append(f"{key}={updates[key]}")
                written.add(key)
            continue  # drop the old line (commented or active); value already placed
        if key in unset_keys and not is_comment:
            continue  # drop only the active line; keep any template comment
        out_lines.append(line)

    for key, value in updates.items():
        if key not in written:
            out_lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    if is_new:
        os.chmod(env_path, 0o600)


def _normalize_path(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.home() / candidate
    return candidate


def _device_of(path: Path) -> int | None:
    """The storage device id of `path`, looked up on its nearest existing
    ancestor (the path itself may not exist yet). Used to detect a mirror that
    sits on the same disk as the library — where it wouldn't survive a disk loss."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return probe.stat().st_dev
    except OSError:
        return None


def on_same_disk(a: Path, b: Path) -> bool:
    """True only when both paths are known to live on the same device."""
    dev_a, dev_b = _device_of(a), _device_of(b)
    return dev_a is not None and dev_a == dev_b


def _ask_path(ask: AskFn, out: OutFn, label: str, default: Path) -> Path:
    out(label)
    out(f"  (default: {default})")
    answer = ask("› ").strip()
    return _normalize_path(answer) if answer else default


def _ask_yes_no(ask: AskFn, out: OutFn, label: str, *, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{label} {hint} ").strip().lower()
    if not answer:
        return default
    return answer[0] in ("y", "o")


def collect_paths(ask: AskFn, out: OutFn) -> dict[str, Path | None]:
    """Ask where the Inbox, Library and (optional) Mirror live. Returns
    {"inbox": Path, "library": Path, "mirror": Path | None}."""
    defaults = default_setup_paths()
    out("\nWhere should ProcraFiler keep your files?")
    out("Press Enter to accept the default, or type your own path.")

    inbox = _ask_path(
        ask, out,
        "\n• Inbox — where you drop the files to be filed:",
        defaults["inbox"],
    )
    library = _ask_path(
        ask, out,
        "\n• Library — where your filed documents are kept:",
        defaults["library"],
    )

    out("\n• Mirror — a backup copy of your library (optional).")
    out("  Strongly recommended: put it on a DIFFERENT disk than the library")
    out("  (e.g. library on SSD, mirror on HDD). On the same disk it does not")
    out("  protect against that disk failing.")
    mirror: Path | None = None
    if _ask_yes_no(ask, out, "  Do you want a backup mirror?", default=True):
        mirror = _ask_path(ask, out, "  Where to put the mirror (ideally another disk)?", defaults["mirror"])

    return {"inbox": inbox, "library": library, "mirror": mirror}


def apply_setup(choices: dict[str, Path | None], *, out: OutFn) -> Path:
    """Persist the chosen paths to the env file, make them effective in THIS
    process, record the mirror choice, and create ONLY the chosen folders.
    Returns the env file path written."""
    inbox = choices["inbox"]
    library = choices["library"]
    mirror = choices["mirror"]
    assert inbox is not None and library is not None  # collected just above

    # 1. Persist to the env file (one source of truth), keeping key + AI chains.
    updates = {_INBOX_ENV: str(inbox), _LIBRARY_ENV: str(library)}
    unset: set[str] = set()
    if mirror is not None:
        updates[_MIRROR_ENV] = str(mirror)
    else:
        unset.add(_MIRROR_ENV)
    env_path = setup_env_path()
    update_env_file(env_path, updates, unset)

    # 2. Make the choice effective in THIS process (the env file is only read on
    #    the NEXT run) so we create exactly the right folders.
    os.environ[_INBOX_ENV] = str(inbox)
    os.environ[_LIBRARY_ENV] = str(library)
    if mirror is not None:
        os.environ[_MIRROR_ENV] = str(mirror)
    paths = default_runtime_paths()

    # 3. Create ONLY the chosen folders (this also makes the config dir exist for
    #    the settings write below).
    ensure_runtime_layout(paths, include_mirror=mirror is not None)

    # 4. Record the mirror choice — the pipeline, doctor and init-layout read it.
    set_feature_flag(paths, "mirror_sync", mirror is not None)

    out(f"\n✓ Paths saved to {env_path}")
    out(f"  • Inbox    : {paths.inbox_dir}")
    out(f"  • Library  : {paths.library_root}")
    out(f"  • Mirror   : {paths.mirror_root}" if mirror is not None else "  • Mirror   : disabled")
    return env_path


def _ask_choice(ask: AskFn, out: OutFn, options: list[str]) -> int:
    """Numbered menu; returns the chosen index. Empty/invalid input → 0 (first =
    the recommended default)."""
    for i, opt in enumerate(options, 1):
        out(f"  {i}) {opt}")
    raw = ask("› ").strip()
    return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(options) else 0


def configure_ai(ask: AskFn, out: OutFn, ask_secret: SecretFn) -> None:
    """Ask which AI provider to use and write the matching tested preset to the
    env file. Default = Mistral API (also offers to store the API key)."""
    out("\nHow should the AI read and classify your documents?")
    choice = _ask_choice(ask, out, [
        "Online — Mistral API (recommended: simple, reliable)",
        "All local — Ollama (free, offline; slower; depends on your VRAM)",
        "I'll configure it myself later (edit the .env)",
    ])
    env_path = setup_env_path()

    if choice == 1:  # all local Ollama
        update_env_file(env_path, dict(_OLLAMA_PRESET))
        out("✓ AI set to local Ollama (gemma4:12b · minicpm-v · qwen2.5vl:7b).")
        out("  Make sure Ollama is running and the models are pulled. Pick models")
        out("  for your VRAM in docs/ai-providers.md. Local is private + free, but slower.")
        return

    if choice == 2:  # configure later
        out(f"  Left as-is — the defaults in {env_path} are the Mistral API.")
        out("  See docs/ai-providers.md for tested models (API + local by VRAM).")
        return

    # choice 0 → Mistral API (the default)
    update_env_file(env_path, dict(_MISTRAL_PRESET))
    out("✓ AI set to the Mistral API (the default).")
    key = ask_secret("Mistral API key (or Enter to add it later): ").strip()
    if key:
        update_env_file(env_path, {"MISTRAL_API_KEY": key})
        out("✓ API key saved.")
    else:
        out(f"  No key yet — add MISTRAL_API_KEY to {env_path} before your first run.")


def setup(*, ask: AskFn = input, out: OutFn = print, ask_secret: SecretFn = getpass.getpass) -> int:
    """Guided first run: choose where files live (Inbox/Library/optional Mirror),
    which AI, then who you are (`setup-context`). Returns a process exit code."""
    out("ProcraFiler — first run")
    out("=" * 23)
    out("Three steps: (1) where your files live, (2) which AI, (3) who you are.")

    paths_saved = False
    try:
        choices = collect_paths(ask, out)

        out("\nSummary:")
        out(f"  • Inbox    : {choices['inbox']}")
        out(f"  • Library  : {choices['library']}")
        out(f"  • Mirror   : {choices['mirror'] if choices['mirror'] is not None else 'disabled'}")

        distinct = {choices["inbox"], choices["library"]} | (
            {choices["mirror"]} if choices["mirror"] is not None else set()
        )
        expected = 3 if choices["mirror"] is not None else 2
        if len(distinct) < expected:
            out("\n⚠ Warning: these paths are not all distinct — not recommended.")

        mirror = choices["mirror"]
        library = choices["library"]
        if mirror is not None and library is not None and on_same_disk(mirror, library):
            out("\n⚠ The mirror seems to be on the SAME disk as the library: it will not")
            out("  protect against that disk failing. A different disk is recommended.")

        if not _ask_yes_no(ask, out, "\nCreate these folders and save?", default=True):
            out("Cancelled — nothing was created or changed.")
            return 1

        apply_setup(choices, out=out)
        paths_saved = True

        # Step 2: which AI reads & classifies the documents.
        out("\n— Step 2: which AI —")
        configure_ai(ask, out, ask_secret)

        # Step 3: who you are (flows straight into the context questionnaire).
        out("\n— Step 3: who are you? (helps the AI file your documents; all optional) —")
        setup_context(ask=ask, out=out)
    except (EOFError, KeyboardInterrupt):
        out("")
        if paths_saved:
            out("Interrupted — your paths are saved. Resume \"who you are\" any time: "
                "procrafiler setup-context")
            return 0
        out("Interrupted — nothing was created or changed.")
        return 1

    out("\n✓ Setup complete. Start filing with: procrafiler process-all")
    out("  Run setup again any time: procrafiler setup")
    return 0
