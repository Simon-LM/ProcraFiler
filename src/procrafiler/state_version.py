"""Which release last wrote this state directory, and what to do about it.

**The gap this closes.** The catalog migrates itself forward: `catalog.init_schema`
adds any column it finds missing, so a base written by 0.6 opens cleanly under
0.11. Nothing goes the other way, and nothing recorded *which* release had written
the state — so an older build could open a newer one's catalog and write into it
without a word.

**How harmful that is today: barely.** `upsert_document` names its columns in
`ON CONFLICT DO UPDATE SET`, so a column an older build knows nothing about keeps
its value on an existing row, and is simply NULL on a new one — which a newer build
already reads as "not computed yet". Nothing is destroyed.

**Why the guard exists anyway.** That safety is a property of the migrations
written so far, all of which merely ADD. The day one changes the *meaning* of an
existing column — a new shape for `content_json`, say — an older build writing the
old shape into it produces two formats under one name, with nothing to tell them
apart. That is silent corruption, and it is not detectable after the fact. The
stamp costs one small file and makes the moment visible instead.

**Deliberately not a schema number.** A number would have to be bumped by whoever
writes the migration, and would be wrong exactly when they forget. The release
version is already recorded by setuptools-scm from the git tag, so it cannot drift
from what is actually running.

**What it does not do.** It never blocks a NEWER build from opening an older
state: that direction is what the migrations are for. It stays silent when either
version is unreadable or is the `0.0.0` setuptools-scm fallback — a checkout with
no tags must not start refusing its own sandbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle broken at runtime
    from procrafiler.config import RuntimePaths

# Set to "1" to run an older release over a newer state on purpose.
ALLOW_OLDER_ENV = "PROCRAFILER_ALLOW_OLDER_VERSION"

STATE_VERSION_FILENAME = "state-version.json"

# What setuptools-scm reports with no tag in sight. It means "unknown", not "very
# old", and treating it as a version would make every untagged checkout refuse.
UNKNOWN_RELEASE = (0, 0, 0)


class StateWrittenByNewerVersion(RuntimeError):
    """An older release was about to write into a newer release's state."""


def state_version_file(paths: RuntimePaths) -> Path:
    return paths.state_root / STATE_VERSION_FILENAME


def running_version() -> str:
    from procrafiler import __version__  # local: keeps this module import-light

    return __version__


def release_of(version: str | None) -> tuple[int, int, int] | None:
    """`(major, minor, micro)` of a version string, or None if it cannot be read.

    Everything after the third number is dropped on purpose: `0.12.0.dev4+g1a2b3c`
    is the same release as `0.12.0` as far as the state is concerned, and refusing
    to run a development build over the state its own release wrote would be
    obstruction rather than protection.
    """
    if not version:
        return None
    numbers: list[int] = []
    for part in version.strip().split("+", 1)[0].split("."):
        if not part.isdigit():
            break
        numbers.append(int(part))
        if len(numbers) == 3:
            break
    if len(numbers) < 3:
        return None
    return (numbers[0], numbers[1], numbers[2])


def recorded_version(paths: RuntimePaths) -> str | None:
    """The version stamped on this state, or None if there is none to read.

    A missing, unreadable or malformed file is not an error: state directories
    written before this existed have no stamp, and they must keep working.
    """
    try:
        payload = json.loads(state_version_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) else None


def guard_state_version(paths: RuntimePaths) -> None:
    """Refuse to write a state that a newer release wrote.

    A no-op when there is no stamp, when either version is unreadable, and when the
    user has deliberately set `PROCRAFILER_ALLOW_OLDER_VERSION=1`.
    """
    if os.environ.get(ALLOW_OLDER_ENV, "").strip() == "1":
        return

    stamped = recorded_version(paths)
    written = release_of(stamped)
    running = release_of(running_version())
    if written is None or running is None:
        return
    if UNKNOWN_RELEASE in (written, running):
        return
    if written <= running:
        return

    raise StateWrittenByNewerVersion(
        "Refusing to run: this state was written by a newer ProcraFiler.\n"
        f"  state        {paths.state_root}\n"
        f"  written by   {stamped}\n"
        f"  running      {running_version()}\n"
        "This release does not know what the newer one stored there, and writing over\n"
        "it can leave the catalog holding two shapes of the same field with nothing to\n"
        "tell them apart. Move back to the newer release instead:\n"
        "  ./scripts/update.sh --mode user      (or --mode system, with sudo)\n"
        f"To run this version over that state anyway: {ALLOW_OLDER_ENV}=1"
    )


def record_state_version(paths: RuntimePaths) -> None:
    """Stamp the state with the running version — upwards only.

    Never downwards, so a run forced through with `PROCRAFILER_ALLOW_OLDER_VERSION`
    cannot erase the mark and quietly make the next older run look legitimate.
    """
    version = running_version()
    running = release_of(version)
    if running is None or running == UNKNOWN_RELEASE:
        return

    written = release_of(recorded_version(paths))
    if written is not None and written >= running:
        return

    try:
        state_version_file(paths).write_text(
            json.dumps({"version": version}, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        # A stamp that cannot be written must never stop a run: it is a guard
        # against a rare downgrade, not a precondition for filing documents.
        return
