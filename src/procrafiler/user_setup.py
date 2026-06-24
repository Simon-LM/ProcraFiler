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

# The three locations the user chooses, mapped to the env vars the app reads.
_INBOX_ENV = "PROCRAFILER_WORKSPACE_DIR"
_LIBRARY_ENV = "PROCRAFILER_LIBRARY_DIR"
_MIRROR_ENV = "PROCRAFILER_LIBRARY_MIRROR_DIR"


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
    out(f"  (défaut : {default})")
    answer = ask("› ").strip()
    return _normalize_path(answer) if answer else default


def _ask_yes_no(ask: AskFn, out: OutFn, label: str, *, default: bool) -> bool:
    hint = "[O/n]" if default else "[o/N]"
    answer = ask(f"{label} {hint} ").strip().lower()
    if not answer:
        return default
    return answer[0] in ("o", "y")


def collect_paths(ask: AskFn, out: OutFn) -> dict[str, Path | None]:
    """Ask where the Inbox, Library and (optional) Mirror live. Returns
    {"inbox": Path, "library": Path, "mirror": Path | None}."""
    defaults = default_setup_paths()
    out("\nOù veux-tu que ProcraFiler range tes fichiers ?")
    out("Valide par Entrée pour accepter le défaut, ou tape ton propre chemin.")

    inbox = _ask_path(
        ask, out,
        "\n• Dépôt (Inbox) — tu y déposes les fichiers à classer :",
        defaults["inbox"],
    )
    library = _ask_path(
        ask, out,
        "\n• Bibliothèque (Library) — tes fichiers classés y sont rangés :",
        defaults["library"],
    )

    out("\n• Miroir (Mirror) — une copie de sauvegarde de ta bibliothèque (optionnel).")
    out("  Fortement conseillé : mets-le sur un AUTRE disque que la bibliothèque")
    out("  (p. ex. bibliothèque sur SSD, miroir sur HDD). Sur le même disque, il ne")
    out("  protège pas contre une panne du disque principal.")
    mirror: Path | None = None
    if _ask_yes_no(ask, out, "  Veux-tu un miroir de sauvegarde ?", default=True):
        mirror = _ask_path(ask, out, "  Où mettre le miroir (idéalement un autre disque) ?", defaults["mirror"])

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

    out(f"\n✓ Chemins enregistrés dans {env_path}")
    out(f"  • Inbox    : {paths.inbox_dir}")
    out(f"  • Library  : {paths.library_root}")
    out(f"  • Mirror   : {paths.mirror_root}" if mirror is not None else "  • Mirror   : désactivé")
    return env_path


def setup(*, ask: AskFn = input, out: OutFn = print) -> int:
    """Guided first run: choose where files live (Inbox/Library/optional Mirror),
    then who you are (`setup-context`). Returns a process exit code."""
    out("ProcraFiler — premier lancement")
    out("=" * 31)
    out("Deux étapes : (1) où ranger tes fichiers, puis (2) qui tu es (pour aider l'IA).")

    paths_saved = False
    try:
        choices = collect_paths(ask, out)

        out("\nRécapitulatif :")
        out(f"  • Inbox    : {choices['inbox']}")
        out(f"  • Library  : {choices['library']}")
        out(f"  • Mirror   : {choices['mirror'] if choices['mirror'] is not None else 'désactivé'}")

        distinct = {choices["inbox"], choices["library"]} | (
            {choices["mirror"]} if choices["mirror"] is not None else set()
        )
        expected = 3 if choices["mirror"] is not None else 2
        if len(distinct) < expected:
            out("\n⚠ Attention : ces chemins ne sont pas tous distincts — c'est déconseillé.")

        mirror = choices["mirror"]
        library = choices["library"]
        if mirror is not None and library is not None and on_same_disk(mirror, library):
            out("\n⚠ Le miroir semble sur le MÊME disque que la bibliothèque : il ne")
            out("  protègera pas d'une panne de ce disque. Un autre disque est conseillé.")

        if not _ask_yes_no(ask, out, "\nOn crée ces dossiers et on enregistre ?", default=True):
            out("Annulé — rien n'a été créé ni modifié.")
            return 1

        apply_setup(choices, out=out)
        paths_saved = True

        # Single first run: flow straight into the context questionnaire (who you are).
        out("\n— Étape 2 : qui es-tu ? (aide l'IA à classer ; tout est facultatif) —")
        setup_context(ask=ask, out=out)
    except (EOFError, KeyboardInterrupt):
        out("")
        if paths_saved:
            out("Interrompu — tes chemins sont enregistrés. Reprends « qui es-tu » "
                "quand tu veux : procrafiler setup-context")
            return 0
        out("Interrompu — rien n'a été créé ni modifié.")
        return 1

    out("\n✓ Configuration terminée. Lance le classement avec : procrafiler process-all")
    out("  Refaire la configuration quand tu veux : procrafiler setup")
    return 0
