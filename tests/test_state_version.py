from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from procrafiler import state_version
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.state_version import (
    ALLOW_OLDER_ENV,
    StateWrittenByNewerVersion,
    guard_state_version,
    record_state_version,
    recorded_version,
    release_of,
    state_version_file,
)


class ReleaseParsingTests(unittest.TestCase):
    def test_a_plain_release(self) -> None:
        self.assertEqual(release_of("0.11.1"), (0, 11, 1))

    def test_a_development_build_is_its_own_release(self) -> None:
        """`0.12.0.dev4+g1a2b3c` must not be refused over the state 0.12.0 wrote:
        that would obstruct the person testing the very release it belongs to."""
        self.assertEqual(release_of("0.12.0.dev4+g1a2b3c"), (0, 12, 0))

    def test_what_cannot_be_read_yields_nothing(self) -> None:
        for value in ("", "unknown", "0.11", "v0.11.1", None):
            with self.subTest(value=value):
                self.assertIsNone(release_of(value))


class _LayoutTestCase(unittest.TestCase):
    """A throwaway layout, with the five root variables pointed at it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "PROCRAFILER_WORKSPACE_DIR": str(self.root / "inbox"),
            "PROCRAFILER_LIBRARY_DIR": str(self.root / "lib"),
            "PROCRAFILER_LIBRARY_MIRROR_DIR": str(self.root / "mirror"),
            "PROCRAFILER_HOME": str(self.root / "state"),
            "PROCRAFILER_CONFIG_HOME": str(self.root / "config"),
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.paths = default_runtime_paths()
        self.paths.state_root.mkdir(parents=True, exist_ok=True)

    def _stamp(self, version: str) -> None:
        state_version_file(self.paths).write_text(
            json.dumps({"version": version}), encoding="utf-8")

    def _running(self, version: str) -> mock._patch[str]:
        return mock.patch.object(state_version, "running_version", return_value=version)


class GuardTests(_LayoutTestCase):
    def test_it_refuses_a_state_written_by_a_newer_release(self) -> None:
        self._stamp("0.12.0")
        with self._running("0.11.1"):
            with self.assertRaises(StateWrittenByNewerVersion) as caught:
                guard_state_version(self.paths)

        message = str(caught.exception)
        self.assertIn("0.12.0", message, "it must say which version wrote the state")
        self.assertIn("0.11.1", message, "it must say which version is running")
        self.assertIn(str(self.paths.state_root), message, "it must name the state it refused")
        self.assertIn(ALLOW_OLDER_ENV, message, "a refusal with no way through is a dead end")

    def test_a_newer_release_opens_an_older_state(self) -> None:
        """The direction the migrations exist for. Refusing it would break every
        ordinary update."""
        self._stamp("0.10.0")
        with self._running("0.11.1"):
            guard_state_version(self.paths)  # must not raise

    def test_the_same_release_is_fine(self) -> None:
        self._stamp("0.11.1")
        with self._running("0.11.1"):
            guard_state_version(self.paths)

    def test_an_unstamped_state_is_fine(self) -> None:
        """Every state directory written before this existed has no stamp, and must
        keep working."""
        with self._running("0.11.1"):
            guard_state_version(self.paths)

    def test_a_corrupt_stamp_is_ignored_rather_than_fatal(self) -> None:
        state_version_file(self.paths).write_text("{not json", encoding="utf-8")
        with self._running("0.11.1"):
            guard_state_version(self.paths)
        self.assertIsNone(recorded_version(self.paths))

    def test_the_untagged_fallback_never_refuses(self) -> None:
        """`0.0.0` is what setuptools-scm reports with no tag in sight. It means
        "unknown", not "very old" — a checkout without tags must not start refusing
        its own sandbox."""
        self._stamp("0.12.0")
        with self._running("0.0.0"):
            guard_state_version(self.paths)

    def test_it_can_be_forced_through_on_purpose(self) -> None:
        self._stamp("0.12.0")
        with self._running("0.11.1"), mock.patch.dict(os.environ, {ALLOW_OLDER_ENV: "1"}):
            guard_state_version(self.paths)


class StampTests(_LayoutTestCase):
    def test_it_stamps_a_state_that_has_none(self) -> None:
        with self._running("0.11.1"):
            record_state_version(self.paths)
        self.assertEqual(recorded_version(self.paths), "0.11.1")

    def test_it_moves_the_stamp_forward(self) -> None:
        self._stamp("0.10.0")
        with self._running("0.11.1"):
            record_state_version(self.paths)
        self.assertEqual(recorded_version(self.paths), "0.11.1")

    def test_it_never_moves_the_stamp_backwards(self) -> None:
        """A run forced through with the escape hatch must not erase the mark, or the
        next older run would look legitimate and meet no refusal at all."""
        self._stamp("0.12.0")
        with self._running("0.11.1"), mock.patch.dict(os.environ, {ALLOW_OLDER_ENV: "1"}):
            record_state_version(self.paths)
        self.assertEqual(recorded_version(self.paths), "0.12.0")

    def test_an_unknown_running_version_stamps_nothing(self) -> None:
        """Writing `0.0.0` would tell the next run that a nonexistent release owns
        this state."""
        with self._running("0.0.0"):
            record_state_version(self.paths)
        self.assertFalse(state_version_file(self.paths).exists())


class WiringTests(_LayoutTestCase):
    """The guard is worth nothing if the entry points do not go through it."""

    def test_building_the_layout_stamps_it(self) -> None:
        with self._running("0.11.1"):
            ensure_runtime_layout(self.paths, include_mirror=False)
        self.assertEqual(recorded_version(self.paths), "0.11.1")

    def test_building_the_layout_is_refused_over_a_newer_state(self) -> None:
        self._stamp("0.12.0")
        with self._running("0.11.1"):
            with self.assertRaises(StateWrittenByNewerVersion):
                ensure_runtime_layout(self.paths, include_mirror=False)

    def test_it_refuses_before_creating_anything(self) -> None:
        """A refusal that arrives after the writing has begun is not a refusal."""
        self._stamp("0.12.0")
        with self._running("0.11.1"):
            with self.assertRaises(StateWrittenByNewerVersion):
                ensure_runtime_layout(self.paths, include_mirror=False)
        self.assertFalse(self.paths.library_root.exists(), "it built the library anyway")
        self.assertFalse(self.paths.inbox_dir.exists(), "it built the inbox anyway")


class CliTests(_LayoutTestCase):
    def test_the_refusal_reaches_the_user_as_a_message_not_a_traceback(self) -> None:
        """The text is addressed to a person and says what to do next. Under a wall
        of stack frames nobody reads it, and the exit code stops being ours."""
        from procrafiler import cli

        self._stamp("0.12.0")
        with self._running("0.11.1"):
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                code = cli.main(["init-layout"])

        self.assertEqual(code, 1)
        self.assertIn("written by a newer ProcraFiler", captured.getvalue())
        self.assertIn(ALLOW_OLDER_ENV, captured.getvalue())

    def test_an_ordinary_command_still_returns_its_own_code(self) -> None:
        """Anti-vacuity: the wrapper must catch a refusal, not swallow everything."""
        from procrafiler import cli

        with self._running("0.11.1"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["init-layout"]), 0)


if __name__ == "__main__":
    unittest.main()
