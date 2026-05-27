# pyright: reportUnknownVariableType=false
from __future__ import annotations

import fcntl
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from procrafiler.cli import main
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.runtime_lock import LOCK_FILENAME, RuntimeLockedError, runtime_lock


class TestRuntimeLock(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_acquire_and_release_creates_lock_file_with_pid(self) -> None:
        lock_path = self.paths.state_root / LOCK_FILENAME
        self.assertFalse(lock_path.exists())

        with runtime_lock(self.paths):
            self.assertTrue(lock_path.exists())
            contents = lock_path.read_text(encoding="ascii").strip()
            self.assertEqual(contents, str(os.getpid()))

        # File remains after release; that's intentional (no race window on
        # unlink). The lock state lives in the fd, not the filesystem entry.
        self.assertTrue(lock_path.exists())

    def test_second_acquisition_blocks_when_first_still_held(self) -> None:
        lock_path = self.paths.state_root / LOCK_FILENAME
        external_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeLockedError) as ctx:
                with runtime_lock(self.paths):
                    self.fail("must not enter the block when lock is held")
            self.assertEqual(ctx.exception.lock_path, str(lock_path))
        finally:
            fcntl.flock(external_fd, fcntl.LOCK_UN)
            os.close(external_fd)

    def test_acquire_succeeds_after_external_holder_releases(self) -> None:
        lock_path = self.paths.state_root / LOCK_FILENAME
        external_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(external_fd, fcntl.LOCK_UN)
        finally:
            os.close(external_fd)

        # External holder is gone; our lock should now go through.
        with runtime_lock(self.paths):
            pass

    def test_cli_process_once_returns_75_when_locked(self) -> None:
        lock_path = self.paths.state_root / LOCK_FILENAME
        external_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (self.paths.inbox_dir / "would-be-processed.pdf").write_bytes(b"x")

            stderr = io.StringIO()
            stdout = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                exit_code = main(["process-once"])

            self.assertEqual(exit_code, 75)
            self.assertIn("already running", stderr.getvalue())
            # The would-be-processed file must still sit untouched in inbox.
            self.assertTrue((self.paths.inbox_dir / "would-be-processed.pdf").exists())
            self.assertEqual(list(self.paths.queue_dir.iterdir()), [])
        finally:
            fcntl.flock(external_fd, fcntl.LOCK_UN)
            os.close(external_fd)


if __name__ == "__main__":
    unittest.main()
