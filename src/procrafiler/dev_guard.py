"""Stop a development build from writing into someone's real library.

**The incident this exists for.** On 2026-07-28 a development run created a full
ProcraFiler layout in the developer's real home directory — inbox, library
taxonomy, mirror, state files. Nothing was lost, because the layout was empty and
no document was ever processed. But nothing in the code prevented it either: the
built-in defaults are rooted in ``$HOME``, and about thirty CLI entry points build
the layout from them. The only thing between a dev session and a real library was
the operator remembering to set five environment variables.

**What is checked, and what is deliberately not.** The question "is this a
development build?" is answered by where the package was imported from, not by
looking for a ``.git`` directory. Git detection was tried and is useless here:
``install-meta.env`` records ``REPO_ROOT`` pointing at the very same checkout,
because production is installed *from* the git tree and updates from it. What does
separate them is that the installer copies the package into its own venv's
``site-packages``, while development uses ``pip install -e``. So a package living
at ``<root>/src/procrafiler`` next to a ``pyproject.toml`` is a source checkout,
and anything else — the installer's copy, or a plain ``pip install procrafiler`` —
is not. That last case matters: a user who installs normally must never meet these
guards.

**Three independent refusals.** Layered on purpose, because each one needs
something different to work and can therefore fail differently:

1. the resolved roots are the *installed* layout (needs ``install-meta.env``);
2. the roots hold real work and carry no sandbox marker (needs existing data);
3. the roots are the built-in defaults (needs nothing — it always applies).

The third is the one that would have stopped the incident, and the only one that
works on a fresh clone on a machine with no installation and no data.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime
    from procrafiler.config import RuntimePaths

# Set to "1" to write to a real layout from a source checkout on purpose.
ALLOW_REAL_DATA_ENV = "PROCRAFILER_ALLOW_REAL_DATA"

# Dropped in the state root the first time a source checkout uses a layout that is
# neither production nor already in use. Its whole job is to let guard 2 tell a
# development sandbox — which legitimately fills up with test documents — from a
# real library whose production marker went missing.
SANDBOX_MARKER_NAME = ".procrafiler-sandbox"

# The environment variables that move the roots away from the built-in defaults.
ROOT_ENV_VARS = (
    "PROCRAFILER_WORKSPACE_DIR",
    "PROCRAFILER_LIBRARY_DIR",
    "PROCRAFILER_LIBRARY_MIRROR_DIR",
    "PROCRAFILER_HOME",
    "PROCRAFILER_CONFIG_HOME",
)


class ProductionWriteRefused(RuntimeError):
    """A source checkout tried to write to a layout that is not its sandbox."""


def source_checkout_root() -> Path | None:
    """The repository root when this package is imported from a source tree.

    ``<root>/src/procrafiler/dev_guard.py`` with a ``<root>/pyproject.toml`` means a
    checkout — whether run through ``pip install -e`` or straight off ``sys.path``.
    The installer's copy lives in its venv's ``site-packages`` and returns None, and
    so does an ordinary ``pip install``.
    """
    package_dir = Path(__file__).resolve().parent
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parent.parent
    return root if (root / "pyproject.toml").is_file() else None


def install_meta_file() -> Path:
    """Where the installer records the installation, mirroring its own layout."""
    return Path.home() / ".local" / "share" / "procrafiler" / "app" / "install-meta.env"


def read_install_meta() -> dict[str, str]:
    """The installer's `KEY=value` record, or an empty mapping when absent.

    Absent is the normal case on a machine that only ever ran from source, so it
    must degrade to "nothing known about an installation" rather than to an error.
    """
    return _parse_env_file(install_meta_file())


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@contextmanager
def _root_env(overrides: dict[str, str]) -> Iterator[None]:
    """Run with exactly `overrides` in force for the five root variables."""
    saved = {name: os.environ.get(name) for name in ROOT_ENV_VARS}
    try:
        for name in ROOT_ENV_VARS:
            os.environ.pop(name, None)
        for name, value in overrides.items():
            if name in ROOT_ENV_VARS:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value


def _paths_for(overrides: dict[str, str], *, force_home_defaults: bool = False) -> RuntimePaths:
    from procrafiler.config import default_runtime_paths  # local: import cycle

    with _root_env(overrides):
        return default_runtime_paths(force_home_defaults=force_home_defaults)


def _roots(paths: RuntimePaths) -> set[Path]:
    """The four roots that hold user data or app state, resolved.

    Resolved, so a symlinked or relative spelling of the same directory cannot slip
    past the comparison.
    """
    return {
        _resolve(paths.workspace_root),
        _resolve(paths.library_root),
        _resolve(paths.mirror_root),
        _resolve(paths.state_root),
    }


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - resolve() barely ever raises
        return path.absolute()


def default_layout_roots() -> set[Path]:
    """The roots an unconfigured PRODUCTION run targets: `$HOME/Downloads/...`.

    `force_home_defaults` is load-bearing and not decoration. A source checkout now
    defaults to its own sandbox rather than to the home, so asking the ordinary way
    would have this guard compare a checkout's roots against the checkout's own
    sandbox — where they always match, and the guard would pass everything. The
    question here is never "where do I default to", it is "where would a real user's
    installation be".
    """
    return _roots(_paths_for({}, force_home_defaults=True))


def installed_layout_roots() -> set[Path]:
    """The roots the installed app uses, or an empty set when nothing is installed.

    Read from the env file the installer points at, so a user who moved their
    library out of `$HOME` is still protected — the check follows their real
    configuration rather than assuming the defaults.
    """
    meta = read_install_meta()
    if not meta:
        return set()
    env_file = meta.get("ENV_FILE", "")
    overrides = _parse_env_file(Path(env_file)) if env_file else {}
    return _roots(_paths_for(overrides))


def layout_holds_real_work(paths: RuntimePaths) -> bool:
    """Has anything ever been filed here?

    Two cheap questions, and no manifest of a pristine install to maintain: a
    document under the library root, or a single line in the action log. An empty
    taxonomy skeleton — which is all `ensure_runtime_layout` creates — answers no.
    """
    library = paths.library_root
    try:
        if library.is_dir():
            for entry in library.rglob("*"):
                if entry.is_file():
                    return True
    except OSError:  # pragma: no cover - unreadable library is not our business here
        pass
    try:
        return paths.actions_log_file.stat().st_size > 0
    except OSError:
        return False


def sandbox_marker_file(paths: RuntimePaths) -> Path:
    return paths.state_root / SANDBOX_MARKER_NAME


def is_marked_sandbox(paths: RuntimePaths) -> bool:
    return sandbox_marker_file(paths).is_file()


def mark_sandbox(paths: RuntimePaths) -> None:
    """Claim a layout as a development sandbox. Best-effort and idempotent.

    Called *after* the directories exist. A failure here must never break a run: the
    only consequence is that guard 2 will ask its question again next time.
    """
    marker = sandbox_marker_file(paths)
    if marker.exists():
        return
    try:
        marker.write_text(
            "This layout was created by a ProcraFiler source checkout and holds test data.\n"
            "Delete this file if it ever becomes a real library.\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _describe(paths: RuntimePaths) -> str:
    return (
        f"  inbox   {paths.workspace_root}\n"
        f"  library {paths.library_root}\n"
        f"  mirror  {paths.mirror_root}\n"
        f"  state   {paths.state_root}"
    )


def _how_to_proceed(checkout: Path) -> str:
    return (
        "A development build writes only where it is explicitly told to:\n"
        f"  {checkout}/sandbox/run.sh\n"
        "or set PROCRAFILER_WORKSPACE_DIR, PROCRAFILER_LIBRARY_DIR,\n"
        "PROCRAFILER_LIBRARY_MIRROR_DIR, PROCRAFILER_HOME, PROCRAFILER_CONFIG_HOME.\n"
        f"To write to this layout on purpose, set {ALLOW_REAL_DATA_ENV}=1."
    )


def guard_mutation(paths: RuntimePaths) -> None:
    """Refuse a source-checkout run that is about to write outside its sandbox.

    A no-op for an installed build, for a normal `pip install`, and when the user
    has deliberately set `PROCRAFILER_ALLOW_REAL_DATA=1`.

    Raises `ProductionWriteRefused`, naming the directories it declined to touch —
    a refusal that does not say *where* it was pointed is impossible to act on.
    """
    if os.environ.get(ALLOW_REAL_DATA_ENV, "").strip() == "1":
        return
    checkout = source_checkout_root()
    if checkout is None:
        return

    targeted = _roots(paths)

    # 1. The installed layout, read from the installer's own record.
    installed = installed_layout_roots()
    if installed and targeted & installed:
        raise ProductionWriteRefused(
            "Refusing to write to the INSTALLED ProcraFiler layout from a source checkout.\n"
            f"{_describe(paths)}\n"
            f"  running   {sys.prefix}\n"
            f"  installed {read_install_meta().get('VENV_DIR', '?')}\n"
            f"{_how_to_proceed(checkout)}"
        )

    # 3. The built-in defaults. Checked before 2 because it needs nothing to work,
    #    so it still fires on a machine with no installation and no data.
    if targeted & default_layout_roots():
        raise ProductionWriteRefused(
            "Refusing to write to the DEFAULT ProcraFiler layout from a source checkout.\n"
            "These are the paths a real user's installation would use.\n"
            f"{_describe(paths)}\n"
            f"{_how_to_proceed(checkout)}"
        )

    # 2. Someone else's library: in use, and never claimed as a sandbox.
    if layout_holds_real_work(paths) and not is_marked_sandbox(paths):
        raise ProductionWriteRefused(
            "Refusing to write to a library that already holds documents, from a source\n"
            "checkout, with no sandbox marker. This looks like real data.\n"
            f"{_describe(paths)}\n"
            f"If it really is a sandbox, create {sandbox_marker_file(paths)}\n"
            f"{_how_to_proceed(checkout)}"
        )
