"""Cross-process advisory locking for the ProcraFiler pipeline.

Two parallel `procrafiler process-all` runs (e.g. cron + a manual launch)
would race on the same inbox: `move()` collisions, doubled DB inserts,
inconsistent mirror state. This module provides a single named lock at
`{state_root}/procrafiler.lock` that mutating commands acquire before
touching anything.

The lock is Linux `flock` (advisory, per-open-file-description). It is
released automatically when the context exits, even on exception or
process death.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator

from procrafiler.config import RuntimePaths


LOCK_FILENAME = "procrafiler.lock"


class RuntimeLockedError(RuntimeError):
    """Raised when another ProcraFiler process is already holding the lock."""

    def __init__(self, lock_path: str) -> None:
        super().__init__(f"another procrafiler process is running (lock: {lock_path})")
        self.lock_path = lock_path


def probe_runtime_lock(paths: RuntimePaths) -> str | None:
    """Is another process holding the lock? Answer without creating anything.

    `runtime_lock` creates the state directory and the lock file, which is right
    for a command that is about to work and wrong for one that is only looking:
    `doctor` used to materialise both just to report that the lock was free.

    A missing state directory or a missing lock file both mean "nobody is
    holding it" — the file only exists once a mutating command has run. Returns
    the lock path when it IS held, None otherwise.
    """
    lock_path = paths.state_root / LOCK_FILENAME
    try:
        fd = os.open(str(lock_path), os.O_RDWR)  # no O_CREAT: never materialise it
    except OSError:
        return None  # absent, or unreadable — either way we are not blocking on it
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return str(lock_path)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@contextlib.contextmanager
def runtime_lock(paths: RuntimePaths) -> Iterator[None]:
    """Acquire the runtime lock or raise RuntimeLockedError immediately.

    The lock file holds the PID of the current holder for diagnostics.
    On exit, we release the flock but do not delete the file — removing
    it opens a race window where another process could create a new file
    and lock it before we observe the unlock.
    """
    paths.state_root.mkdir(parents=True, exist_ok=True)
    lock_path = paths.state_root / LOCK_FILENAME

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeLockedError(str(lock_path)) from exc

        # Record the holder PID for `procrafiler status`-style diagnostics.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)
