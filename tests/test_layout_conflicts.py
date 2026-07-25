# pyright: reportUnknownVariableType=false
"""Configured roots must never be nested — item C of docs/pre-prod-hardening.md.

`setup` invited free-form paths and only compared them for EXACT equality, so the
most damaging case sailed through: one root inside another. Put the mirror inside
the library and the library walk swallows the mirror — its copies get renamed,
phantom duplicate rows enter the catalog, a `Mirror/Mirror/` level appears, and
every unknown mirror file costs a paid AI call. `rescan.walk_library_files` states
the assumption in writing ("the mirror lives OUTSIDE library_root already"), true
of the DEFAULTS only.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.config import default_runtime_paths, layout_conflicts
from procrafiler.doctor import STATUS_FAIL, STATUS_OK, STATUS_WARN, check_layout, overall_exit_code
from procrafiler.user_setup import _collect_valid_paths, conflicts_for_choices


class _EnvCase(unittest.TestCase):
    KEYS = (
        "PROCRAFILER_WORKSPACE_DIR",
        "PROCRAFILER_LIBRARY_DIR",
        "PROCRAFILER_LIBRARY_MIRROR_DIR",
        "PROCRAFILER_HOME",
        "PROCRAFILER_CONFIG_HOME",
    )

    def setUp(self) -> None:
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _configure(self, *, workspace: str, library: str, mirror: str) -> None:
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(self.root / workspace)
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(self.root / library)
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(self.root / mirror)
        os.environ["PROCRAFILER_HOME"] = str(self.root / "state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(self.root / "config")


class TestLayoutConflicts(_EnvCase):
    def test_a_sane_layout_has_no_conflicts(self) -> None:
        self._configure(workspace="ws", library="Library", mirror="Mirror")
        self.assertEqual(layout_conflicts(default_runtime_paths()), [])

    def test_the_app_defaults_have_no_conflicts(self) -> None:
        """The shipped defaults must pass their own guard."""
        for key in self.KEYS:
            os.environ.pop(key, None)
        self.assertEqual(layout_conflicts(default_runtime_paths()), [])

    def test_mirror_inside_library_is_reported(self) -> None:
        self._configure(workspace="ws", library="Library", mirror="Library/Mirror")
        conflicts = layout_conflicts(default_runtime_paths())
        self.assertEqual(len(conflicts), 1)
        self.assertIn("Mirror is inside Library", conflicts[0])

    def test_library_inside_inbox_workspace_is_reported(self) -> None:
        self._configure(workspace="ws", library="ws/Library", mirror="Mirror")
        conflicts = layout_conflicts(default_runtime_paths())
        self.assertTrue(any("inside" in c for c in conflicts), conflicts)

    def test_identical_paths_are_reported(self) -> None:
        self._configure(workspace="same", library="same", mirror="Mirror")
        conflicts = layout_conflicts(default_runtime_paths())
        self.assertTrue(any("are the same folder" in c for c in conflicts), conflicts)

    def test_state_dir_inside_the_library_is_reported(self) -> None:
        """The derived roots the user never types are exactly where an innocent
        choice bites: app state swallowed by the library."""
        self._configure(workspace="ws", library="Library", mirror="Mirror")
        os.environ["PROCRAFILER_HOME"] = str(self.root / "Library" / "state")
        conflicts = layout_conflicts(default_runtime_paths())
        self.assertTrue(any("App state is inside Library" in c for c in conflicts), conflicts)

    def test_paths_are_compared_resolved(self) -> None:
        """`~/lib` and `~/./lib/` are the same place; the guard must not be fooled."""
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(self.root / "ws")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(self.root / "Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(self.root / "." / "Library" / "Mirror")
        os.environ["PROCRAFILER_HOME"] = str(self.root / "state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(self.root / "config")
        self.assertNotEqual(layout_conflicts(default_runtime_paths()), [])

    def test_a_disabled_mirror_is_excluded_from_the_check(self) -> None:
        """With no mirror, its default path must not raise a false conflict."""
        self._configure(workspace="ws", library="Library", mirror="Library/Mirror")
        self.assertEqual(layout_conflicts(default_runtime_paths(), include_mirror=False), [])


class TestDoctorLayoutCheck(_EnvCase):
    def test_doctor_fails_on_a_nested_layout(self) -> None:
        self._configure(workspace="ws", library="Library", mirror="Library/Mirror")
        paths = default_runtime_paths()
        checks = check_layout(paths)
        nested = [c for c in checks if c.name == "roots_not_nested"]
        self.assertEqual(nested[0].status, STATUS_FAIL)
        self.assertEqual(overall_exit_code(checks), 1)

    def test_doctor_is_ok_on_a_sane_layout(self) -> None:
        self._configure(workspace="ws", library="Library", mirror="Mirror")
        checks = check_layout(default_runtime_paths())
        nested = [c for c in checks if c.name == "roots_not_nested"]
        self.assertEqual(nested[0].status, STATUS_OK)

    def test_doctor_warns_when_the_mirror_shares_the_library_disk(self) -> None:
        """setup says this once at creation; doctor must keep saying it."""
        self._configure(workspace="ws", library="Library", mirror="Mirror")
        checks = check_layout(default_runtime_paths())
        same_disk = [c for c in checks if c.name == "mirror_separate_disk"]
        # Both live in the same tmpdir, hence the same device.
        self.assertEqual(same_disk[0].status, STATUS_WARN)

    def test_disabled_mirror_skips_the_disk_check(self) -> None:
        self._configure(workspace="ws", library="Library", mirror="Mirror")
        checks = check_layout(default_runtime_paths(), mirror_enabled=False)
        same_disk = [c for c in checks if c.name == "mirror_separate_disk"]
        self.assertEqual(same_disk[0].status, "SKIP")


class TestSetupRefusesNesting(_EnvCase):
    """`setup` must REFUSE a nested layout, not warn about it."""

    def _answers(self, *values: str):
        it = iter(values)
        return lambda _prompt="": next(it, "")

    def test_conflicts_for_choices_sees_nesting_before_anything_is_created(self) -> None:
        conflicts = conflicts_for_choices(
            {
                "inbox": self.root / "ws",
                "library": self.root / "Library",
                "mirror": self.root / "Library" / "Mirror",
            }
        )
        self.assertTrue(any("Mirror is inside Library" in c for c in conflicts), conflicts)

    def test_conflicts_for_choices_restores_the_environment(self) -> None:
        """It probes via env vars — it must leave no trace behind."""
        os.environ["PROCRAFILER_LIBRARY_DIR"] = "/sentinel/library"
        conflicts_for_choices(
            {"inbox": self.root / "ws", "library": self.root / "L", "mirror": None}
        )
        self.assertEqual(os.environ["PROCRAFILER_LIBRARY_DIR"], "/sentinel/library")

    def test_setup_reasks_then_accepts_a_corrected_layout(self) -> None:
        out_lines: list[str] = []
        ask = self._answers(
            # attempt 1 — mirror nested inside the library → refused
            str(self.root / "ws"), str(self.root / "Library"), "y", str(self.root / "Library" / "M"),
            # attempt 2 — corrected
            str(self.root / "ws"), str(self.root / "Library"), "y", str(self.root / "Mirror"),
        )
        choices = _collect_valid_paths(ask, out_lines.append)

        self.assertIsNotNone(choices)
        assert choices is not None
        self.assertEqual(choices["mirror"], self.root / "Mirror")
        self.assertTrue(
            any("overlap" in line for line in out_lines),
            "the user was never told why the first layout was refused",
        )

    def test_setup_gives_up_after_repeated_overlaps(self) -> None:
        nested = [str(self.root / "ws"), str(self.root / "Library"), "y", str(self.root / "Library" / "M")]
        ask = self._answers(*(nested * 3))
        choices = _collect_valid_paths(ask, lambda _m: None)
        self.assertIsNone(choices, "setup must not accept a nested layout")


if __name__ == "__main__":
    unittest.main()
