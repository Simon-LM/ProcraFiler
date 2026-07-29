# pyright: reportUnknownVariableType=false
"""A command that only looks must not change the machine.

`doctor` is the command you run to decide whether to trust the app with your
documents. It used to call `ensure_runtime_layout()` first, so it **created** the
45-odd directories it was about to check, and then reported them `[OK]`. Pointed
at a mistyped library path it created that path too. The consequence is not
cosmetic: `doctor.py`'s `FAIL "missing: {path}"` branch was unreachable by
construction, so `doctor` could never tell you your library had disappeared.

Two collaborators had to stop creating as well: the runtime lock (a diagnostic was
acquiring the real lock, which materialises the state directory and the lock file)
and the search index (opening it under a missing state directory raises
`unable to open database file`).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.doctor import STATUS_FAIL, STATUS_OK, check_paths, check_runtime_lock, run_doctor
from procrafiler.runtime_lock import probe_runtime_lock, runtime_lock

# Commands that report state and must leave the disk exactly as they found it.
READ_ONLY_COMMANDS = (
    ["status"],
    ["features"],
    ["policy-effective"],
    ["doctor"],
    ["search", "anything"],
    ["deleted-history"],
)


class _VirginLayout(unittest.TestCase):
    """A configured layout whose directories do not exist yet."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "PROCRAFILER_WORKSPACE_DIR", "PROCRAFILER_LIBRARY_DIR",
                "PROCRAFILER_LIBRARY_MIRROR_DIR", "PROCRAFILER_HOME",
                "PROCRAFILER_CONFIG_HOME",
            )
        }
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(self.root / "inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(self.root / "lib")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(self.root / "mirror")
        os.environ["PROCRAFILER_HOME"] = str(self.root / "state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(self.root / "config")
        self.paths = default_runtime_paths()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def entries(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*"))


class TestReadOnlyCommandsCreateNothing(_VirginLayout):
    def test_none_of_them_touches_the_disk(self) -> None:
        from procrafiler.cli import main

        for argv in READ_ONLY_COMMANDS:
            with self.subTest(command=" ".join(argv)):
                before = self.entries()
                try:
                    main(argv)
                except SystemExit:
                    pass
                self.assertEqual(
                    self.entries(), before,
                    f"`{' '.join(argv)}` created {[str(p) for p in set(self.entries()) - set(before)]}",
                )

    def test_search_says_there_is_no_catalog_instead_of_crashing(self) -> None:
        """It used to raise `unable to open database file`: the search index is
        created next to the catalog, and its directory did not exist."""
        from procrafiler.cli import main

        self.assertEqual(main(["search", "invoice"]), 0)


class TestDoctorReportsWhatIsMissing(_VirginLayout):
    def test_a_never_created_layout_gets_one_actionable_line(self) -> None:
        """"Never set up" and "something disappeared" are different problems.
        Nine identical failures for a first run is noise; one line is an answer."""
        checks = check_paths(self.paths)
        self.assertEqual(len(checks), 1, [c.name for c in checks])
        self.assertEqual(checks[0].status, STATUS_FAIL)
        self.assertIn("not created yet", checks[0].message)
        self.assertIn(str(self.paths.library_root), checks[0].message)

    def test_a_library_that_disappeared_is_named(self) -> None:
        """The alarming case, and the one the old code could never report: the app
        was set up, and now a root is gone — an unmounted disk, a mistyped path, a
        deleted folder. Each root is reported individually here."""
        ensure_runtime_layout(self.paths)
        import shutil

        shutil.rmtree(self.paths.library_root)

        by_name = {c.name: c for c in check_paths(self.paths)}
        self.assertEqual(by_name["library_root"].status, STATUS_FAIL)
        self.assertIn("missing", by_name["library_root"].message)
        self.assertEqual(by_name["workspace_root"].status, STATUS_OK)

    def test_doctor_does_not_recreate_the_missing_library(self) -> None:
        """Recreating it is what hid the problem in the first place."""
        ensure_runtime_layout(self.paths)
        import shutil

        shutil.rmtree(self.paths.library_root)
        run_doctor(self.paths)
        self.assertFalse(self.paths.library_root.exists())

    def test_doctor_fails_on_a_mistyped_library_path(self) -> None:
        """End to end: the scenario that motivated the whole item. `doctor` used to
        create the mistyped directory and report it `[OK]`."""
        from procrafiler.cli import main

        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(self.root / "typo-in-my-path")
        self.assertNotEqual(main(["doctor"]), 0)
        self.assertFalse((self.root / "typo-in-my-path").exists())


class TestLockProbeDoesNotMaterialiseIt(_VirginLayout):
    def test_probing_a_layout_with_no_state_creates_nothing(self) -> None:
        self.assertIsNone(probe_runtime_lock(self.paths))
        self.assertFalse(self.paths.state_root.exists())

    def test_the_doctor_check_creates_no_lock_file(self) -> None:
        ensure_runtime_layout(self.paths)
        lock_file = self.paths.state_root / "procrafiler.lock"
        lock_file.unlink(missing_ok=True)

        checks = check_runtime_lock(self.paths)
        self.assertEqual(checks[0].status, STATUS_OK)
        self.assertFalse(lock_file.exists(), "a diagnostic created the lock file")

    def test_a_held_lock_is_still_detected(self) -> None:
        """Anti-vacuity: the probe must not report "free" for everything."""
        ensure_runtime_layout(self.paths)
        with runtime_lock(self.paths):
            self.assertIsNotNone(probe_runtime_lock(self.paths))
        # …and released once the holder is gone.
        self.assertIsNone(probe_runtime_lock(self.paths))


if __name__ == "__main__":
    unittest.main()
