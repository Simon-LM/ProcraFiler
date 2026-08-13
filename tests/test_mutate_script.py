"""The mutation harness must not lie about its own results.

Its verdicts decide whether a guarantee is considered tested, so a harness that
scores an unapplied mutant as caught, or that leaves mutated bytecode behind,
corrupts every campaign run after it — silently, and in the direction of
reassurance.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mutate  # noqa: E402


class BytecodeTrapTests(unittest.TestCase):
    """The defect this script exists to prevent.

    Python validates a cached `.pyc` on the source's size and mtime. A mutation of
    the SAME BYTE LENGTH restored within the same second leaves the file with
    exactly the size and mtime the cache was built from, so Python keeps serving
    the MUTATED bytecode after the restore. It cost a real debugging session: a
    passing test began failing with the source visibly correct on disk.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pkg = Path(self.tmp.name)
        self.module = self.pkg / "sample.py"
        self.module.write_text("VALUE = 30\n", encoding="utf-8")
        self.cache = self.pkg / "__pycache__"
        self.cache.mkdir()
        self.stale = self.cache / f"sample.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"
        self.stale.write_bytes(b"stale bytecode")

    def test_writing_a_source_drops_its_cached_bytecode(self) -> None:
        mutate.write_source(self.module, "VALUE = 90\n")
        self.assertFalse(self.stale.exists(), "the mutated bytecode would still be served")

    def test_restoring_a_same_length_mutation_also_drops_it(self) -> None:
        """The exact shape of the trap: `30` and `90` occupy the same bytes, so the
        restore alone changes neither size nor (within one second) mtime."""
        mutate.write_source(self.module, "VALUE = 90\n")
        self.stale.write_bytes(b"bytecode built from the mutant")

        mutate.write_source(self.module, "VALUE = 30\n")

        self.assertFalse(self.stale.exists(), "the restore left the mutant's bytecode in place")
        self.assertEqual(self.module.read_text(encoding="utf-8"), "VALUE = 30\n")

    def test_another_modules_cache_is_left_alone(self) -> None:
        """Anti-vacuity: dropping the whole cache directory would work too, and would
        make every unrelated module recompile on every mutant."""
        other = self.cache / f"elsewhere.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"
        other.write_bytes(b"someone else's bytecode")

        mutate.write_source(self.module, "VALUE = 90\n")

        self.assertTrue(other.exists())

    def test_the_test_subprocess_never_writes_bytecode(self) -> None:
        """The second protection. Deleting a stale cache is useless if the run that
        follows writes a fresh one from the mutated source."""
        script = self.pkg / "shows_env.py"
        script.write_text(
            "import os, sys; sys.stderr.write(os.environ.get('PYTHONDONTWRITEBYTECODE', '')); raise SystemExit(1)\n",
            encoding="utf-8",
        )
        # Neutralise the ambient value: this test may itself be running inside a
        # mutation campaign, whose subprocess already carries the flag — and then
        # `{**os.environ}` would satisfy the assertion without the code setting
        # anything. The survivor that revealed it was exactly this.
        self.addCleanup(os.environ.pop, "PYTHONDONTWRITEBYTECODE", None)
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        captured: dict[str, object] = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured["env"] = kwargs.get("env", {})
            return real_run([sys.executable, "-c", "pass"], **{**kwargs, "env": None})

        subprocess.run = spy  # type: ignore[assignment]
        try:
            mutate.run_test("tests.does_not_matter", cwd=self.pkg)
        finally:
            subprocess.run = real_run  # type: ignore[assignment]

        self.assertEqual(dict(captured["env"]).get("PYTHONDONTWRITEBYTECODE"), "1")  # type: ignore[arg-type]


class VerdictTests(unittest.TestCase):
    """A campaign that flatters itself is worse than no campaign."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / "target.py"
        self.target.write_text("VALUE = 30\n", encoding="utf-8")

    def _mutant(self, **over: str) -> mutate.Mutant:
        fields = {"label": "m", "file": "target.py", "old": "VALUE = 30",
                  "new": "VALUE = 90", "test": "tests.whatever"}
        fields.update(over)
        return mutate.Mutant(**fields)  # type: ignore[arg-type]

    def _campaign(self, mutant: mutate.Mutant, *, passes: bool) -> tuple[int, int]:
        real = mutate.run_test
        mutate.run_test = lambda module, *, cwd: passes  # type: ignore[assignment]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return mutate.run_campaign([mutant], root=self.root)
        finally:
            mutate.run_test = real  # type: ignore[assignment]

    def test_a_failing_test_means_the_mutant_was_caught(self) -> None:
        self.assertEqual(self._campaign(self._mutant(), passes=False), (1, 0))

    def test_a_passing_test_means_the_mutant_survived(self) -> None:
        self.assertEqual(self._campaign(self._mutant(), passes=True), (0, 1))

    def test_an_anchor_that_matches_nothing_is_never_scored_as_caught(self) -> None:
        """It proves nothing: the defect was never introduced. Counting it as a pass
        is exactly how a campaign reports coverage it does not have."""
        caught, survived = self._campaign(self._mutant(old="NOT IN THE FILE"), passes=False)
        self.assertEqual((caught, survived), (0, 1))

    def test_an_anchor_that_matches_twice_is_refused(self) -> None:
        self.target.write_text("VALUE = 30\nOTHER = 30\n", encoding="utf-8")
        caught, survived = self._campaign(self._mutant(old="30"), passes=False)
        self.assertEqual((caught, survived), (0, 1))

    def test_the_source_is_restored_whatever_the_verdict(self) -> None:
        for passes in (True, False):
            with self.subTest(passes=passes):
                self._campaign(self._mutant(), passes=passes)
                self.assertEqual(self.target.read_text(encoding="utf-8"), "VALUE = 30\n")

    def test_the_source_is_restored_even_when_the_run_raises(self) -> None:
        real = mutate.run_test

        def boom(module, *, cwd):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt

        mutate.run_test = boom  # type: ignore[assignment]
        try:
            with self.assertRaises(KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
                mutate.run_campaign([self._mutant()], root=self.root)
        finally:
            mutate.run_test = real  # type: ignore[assignment]
        self.assertEqual(self.target.read_text(encoding="utf-8"), "VALUE = 30\n")


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mutants.json"

    def test_a_mutant_missing_a_field_is_refused_by_name(self) -> None:
        self.path.write_text(json.dumps([{"label": "m", "file": "f", "old": "a"}]), encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            mutate.load_mutants(self.path)
        self.assertIn("new", str(caught.exception))
        self.assertIn("test", str(caught.exception))

    def test_a_well_formed_file_loads(self) -> None:
        self.path.write_text(json.dumps([
            {"label": "m", "file": "f.py", "old": "a", "new": "b", "test": "tests.t"}
        ]), encoding="utf-8")
        loaded = mutate.load_mutants(self.path)
        self.assertEqual([m.label for m in loaded], ["m"])

    def test_walking_up_for_the_root_stops_at_the_filesystem_top(self) -> None:
        """Without the stop condition, a path with no `.git` above it spins for ever
        at `/` — which is not a crash but a pegged core, and is how this cost an
        interrupted session before."""
        with self.assertRaises(SystemExit):
            mutate.repo_root(Path(self.tmp.name))


if __name__ == "__main__":
    unittest.main()
