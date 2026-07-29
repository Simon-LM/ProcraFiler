# pyright: reportUnknownVariableType=false
"""Durability against REAL processes — item G of docs/pre-prod-hardening.md.

Two things an in-process test cannot prove:

- **`SIGKILL`.** Raising `KeyboardInterrupt` still unwinds the stack and runs
  `finally` blocks. A real kill does none of that: it is the honest test of "the
  power went out mid-run". The conservation invariant must hold there too.
- **Concurrency.** Patching the lock proves the lock class works; it does not
  prove two `procrafiler` processes racing on one Inbox behave. Only two real
  processes do.

Both spawn subprocesses, so they are slower than the rest of the suite — still
offline (`PROCRAFILER_ENV_FILE=/dev/null` guarantees no key, no chains) and
deterministic.
"""
from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _SubprocessWorkspace(unittest.TestCase):
    """A workspace driven by real child processes, each fully isolated by env."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {
            **os.environ,
            # Authoritative and empty: the child can never load a real key/chain.
            "PROCRAFILER_ENV_FILE": "/dev/null",
            "PROCRAFILER_WORKSPACE_DIR": str(self.root / "ws"),
            "PROCRAFILER_LIBRARY_DIR": str(self.root / "Library"),
            "PROCRAFILER_LIBRARY_MIRROR_DIR": str(self.root / "Mirror"),
            "PROCRAFILER_HOME": str(self.root / "state"),
            "PROCRAFILER_CONFIG_HOME": str(self.root / "config"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
        self.inbox = self.root / "ws" / "Inbox"
        self.queue = self.root / "ws" / "Queue"
        self.library = self.root / "Library"
        prepared = self._run_python(
            "from procrafiler.config import default_runtime_paths, ensure_runtime_layout\n"
            "ensure_runtime_layout(default_runtime_paths())"
        )
        assert prepared.returncode == 0, f"workspace setup failed: {prepared.stderr}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_python(self, code: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            env=self.env, cwd=str(self.root), capture_output=True, text=True, **kwargs
        )

    def _all_files(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))

    def _accounted(self) -> list[Path]:
        return (
            self._all_files(self.inbox)
            + self._all_files(self.queue)
            + self._all_files(self.library)
            + self._all_files(self.root / "ws" / "Inbox_Trash_Manual")
        )


class TestSigkillDurability(_SubprocessWorkspace):
    def test_a_real_kill_mid_run_loses_nothing(self) -> None:
        """Kill -9 during processing, then run again: every document is still there,
        byte for byte, and nothing is stranded."""
        inputs = {}
        for i in range(4):
            path = self.inbox / f"doc_{i}.txt"
            path.write_text(f"document body number {i}")
            inputs[path.name] = _sha256(path)

        # A child that starts processing, then hangs forever inside the read step
        # so we can kill it at a known, mid-flight point.
        with subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent("""
                import time, procrafiler.pipeline as pl
                from procrafiler.config import default_runtime_paths
                real = pl._read_and_analyze
                calls = {"n": 0}
                def slow(*a, **k):
                    calls["n"] += 1
                    if calls["n"] == 2:
                        print("READY", flush=True)
                        time.sleep(300)          # hang mid-run, awaiting the kill
                    return real(*a, **k)
                pl._read_and_analyze = slow
                pl.process_all_inbox_files(default_runtime_paths())
            """)],
            env=self.env, cwd=str(self.root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as child:
            try:
                deadline = time.time() + 60
                while time.time() < deadline:
                    if (child.stdout.readline() or "").strip() == "READY":
                        break
                else:
                    self.fail("the child never reached the mid-run point")
                os.kill(child.pid, signal.SIGKILL)
                child.wait(timeout=30)
            finally:
                # `with Popen` waits on exit; a child still sleeping would hang it.
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)

        self.assertEqual(child.returncode, -signal.SIGKILL, "the child was not really killed")

        # Nothing vanished at the moment of the kill.
        self.assertEqual(
            len(self._accounted()), 4,
            f"after SIGKILL: {[p.name for p in self._accounted()]}",
        )

        # A following run recovers and settles everything.
        result = self._run_python(
            "from procrafiler.config import default_runtime_paths\n"
            "from procrafiler.pipeline import process_all_inbox_files\n"
            "process_all_inbox_files(default_runtime_paths())"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._all_files(self.queue), [], "documents left stranded in the Queue")

        # Content integrity: every original hash is still on disk somewhere.
        on_disk = {_sha256(p) for p in self._accounted()}
        for name, digest in inputs.items():
            self.assertIn(digest, on_disk, f"{name} was lost or corrupted by the kill")


class TestConcurrentProcesses(_SubprocessWorkspace):
    def test_two_real_runs_on_one_inbox_do_not_race(self) -> None:
        """One proceeds, the other is refused by the runtime lock — and no document
        is processed twice or lost."""
        for i in range(3):
            (self.inbox / f"doc_{i}.txt").write_text(f"body {i}")

        code = textwrap.dedent("""
            import sys
            from procrafiler.config import default_runtime_paths
            from procrafiler.pipeline import process_all_inbox_files
            from procrafiler.runtime_lock import RuntimeLockedError, runtime_lock
            paths = default_runtime_paths()
            try:
                with runtime_lock(paths):
                    process_all_inbox_files(paths)
                print("RAN")
            except RuntimeLockedError:
                print("REFUSED")
                sys.exit(75)
        """)
        with subprocess.Popen(
            [sys.executable, "-c", code], env=self.env, cwd=str(self.root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as first, subprocess.Popen(
            [sys.executable, "-c", code], env=self.env, cwd=str(self.root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as second:
            out1, err1 = first.communicate(timeout=120)
            out2, err2 = second.communicate(timeout=120)

        outcomes = sorted([out1.strip(), out2.strip()])
        # Either they genuinely overlapped (one refused), or the first finished
        # before the second started (both ran, sequentially). Both are correct;
        # what must NEVER happen is a document processed twice or lost.
        self.assertIn("RAN", outcomes, f"neither run proceeded: {outcomes} / {err1} / {err2}")

        filed = self._all_files(self.library)
        trashed = self._all_files(self.root / "ws" / "Inbox_Trash_Manual")
        self.assertEqual(
            len(self._accounted()), 3,
            f"documents lost or duplicated: filed={[p.name for p in filed]} "
            f"trashed={[p.name for p in trashed]}",
        )
        self.assertEqual(self._all_files(self.inbox), [], "the Inbox was not drained")
        self.assertEqual(self._all_files(self.queue), [], "documents stranded in the Queue")

    def test_the_lock_is_released_after_a_kill(self) -> None:
        """A killed run must not leave the lock held — that would wedge the app."""
        with subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent("""
                import time
                from procrafiler.config import default_runtime_paths
                from procrafiler.runtime_lock import runtime_lock
                with runtime_lock(default_runtime_paths()):
                    print("HELD", flush=True)
                    time.sleep(300)
            """)],
            env=self.env, cwd=str(self.root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(), "HELD")
                os.kill(child.pid, signal.SIGKILL)
                child.wait(timeout=30)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)

        result = self._run_python("""
            from procrafiler.config import default_runtime_paths
            from procrafiler.runtime_lock import runtime_lock
            with runtime_lock(default_runtime_paths()):
                print("ACQUIRED")
        """)
        self.assertIn("ACQUIRED", result.stdout, f"the lock stayed held after a kill: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
