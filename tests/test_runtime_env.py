from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from procrafiler.runtime_env import (
    _running_under_test_runner,
    default_env_candidates,
    load_runtime_env,
)


class TestRuntimeEnv(unittest.TestCase):
    def setUp(self) -> None:
        # These tests deliberately mutate os.environ (load keys/chains). Snapshot
        # it and restore on teardown so they never leak into other tests — a leak
        # like that is what let the suite reach the live Mistral API.
        self._env_snapshot = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_snapshot)

    def test_load_runtime_env_from_candidate_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "procrafiler.env"
            env_file.write_text(
                "# comment\n"
                "PROCRAFILER_AI_ANALYSIS_PRIMARY=mistral:mistral-small-2506\n"
                "MISTRAL_API_KEY=abc123\n",
                encoding="utf-8",
            )

            os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
            os.environ.pop("MISTRAL_API_KEY", None)
            os.environ.pop("PROCRAFILER_ENV_LOADED_FROM", None)

            loaded = load_runtime_env([env_file])

            self.assertEqual(loaded, env_file)
            self.assertEqual(os.environ.get("PROCRAFILER_AI_ANALYSIS_PRIMARY"), "mistral:mistral-small-2506")
            self.assertEqual(os.environ.get("MISTRAL_API_KEY"), "abc123")
            self.assertEqual(os.environ.get("PROCRAFILER_ENV_LOADED_FROM"), str(env_file))

    def test_does_not_override_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "procrafiler.env"
            env_file.write_text("MISTRAL_API_KEY=file_value\n", encoding="utf-8")

            os.environ["MISTRAL_API_KEY"] = "existing_value"
            load_runtime_env([env_file])

            self.assertEqual(os.environ.get("MISTRAL_API_KEY"), "existing_value")


class TestOfflineSafetyGuard(unittest.TestCase):
    """The cwd `./.env` (dev convenience: real Mistral key + chains) must never be
    auto-loaded under a test runner, whatever the invocation — otherwise an
    "offline" unit test silently hits the live API."""

    def test_we_are_detected_as_a_test_runner(self) -> None:
        self.assertTrue(_running_under_test_runner())

    def test_cwd_dotenv_is_skipped_under_a_test_runner(self) -> None:
        self.assertNotIn(Path.cwd() / ".env", default_env_candidates())

    def test_cwd_dotenv_is_used_by_the_real_app(self) -> None:
        # Simulate a real (non-test) process: __main__ is the app entry point and
        # pytest is not imported. The cwd .env returns as a candidate.
        # PROCRAFILER_ENV_FILE must be cleared too: an explicit file is
        # authoritative and suppresses every other candidate, and the suite
        # bootstrap sets one for the whole run.
        fake_main = types.ModuleType("__main__")
        fake_main.__file__ = "/opt/app/procrafiler/__main__.py"
        with mock.patch.dict(sys.modules):  # snapshot + restore sys.modules
            sys.modules.pop("pytest", None)
            sys.modules["__main__"] = fake_main
            with mock.patch.dict(os.environ):
                os.environ.pop("PROCRAFILER_ENV_FILE", None)
                self.assertFalse(_running_under_test_runner())
                self.assertIn(Path.cwd() / ".env", default_env_candidates())


class TestExplicitEnvFileIsAuthoritative(unittest.TestCase):
    """Naming a file in PROCRAFILER_ENV_FILE must suppress every other candidate.

    The old code searched on when the named file was not a REGULAR file, which is
    exactly what `/dev/null` is not — so the documented way to force an offline
    run instead loaded the developer's real `./.env`, live API key included. The
    same fall-through swallowed any typo in the path.
    """

    def setUp(self) -> None:
        self.saved = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def test_an_explicit_file_is_the_only_candidate(self) -> None:
        os.environ["PROCRAFILER_ENV_FILE"] = str(self.dir / "mine.env")
        self.assertEqual(default_env_candidates(), [self.dir / "mine.env"])

    def test_dev_null_forces_an_offline_run(self) -> None:
        """The whole point: /dev/null loads nothing AND stops the search."""
        os.environ["PROCRAFILER_ENV_FILE"] = "/dev/null"
        os.environ.pop("MISTRAL_API_KEY", None)
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)

        loaded = load_runtime_env()

        self.assertEqual(loaded, Path("/dev/null"))
        self.assertNotIn("MISTRAL_API_KEY", os.environ)
        self.assertNotIn("PROCRAFILER_AI_ANALYSIS_PRIMARY", os.environ)

    def test_an_unreadable_explicit_file_loads_nothing_and_falls_through_to_nothing(self) -> None:
        os.environ["PROCRAFILER_ENV_FILE"] = str(self.dir / "typo.env")  # does not exist
        os.environ.pop("MISTRAL_API_KEY", None)
        os.environ.pop("PROCRAFILER_ENV_LOADED_FROM", None)

        self.assertIsNone(load_runtime_env())
        self.assertNotIn("MISTRAL_API_KEY", os.environ)
        self.assertNotIn("PROCRAFILER_ENV_LOADED_FROM", os.environ)

    def test_a_directory_named_as_the_env_file_is_not_loaded(self) -> None:
        os.environ["PROCRAFILER_ENV_FILE"] = str(self.dir)
        self.assertIsNone(load_runtime_env())

    def test_a_real_explicit_file_is_loaded(self) -> None:
        env_file = self.dir / "mine.env"
        env_file.write_text("PROCRAFILER_TEST_MARKER=set-by-explicit-file\n", encoding="utf-8")
        os.environ["PROCRAFILER_ENV_FILE"] = str(env_file)
        os.environ.pop("PROCRAFILER_TEST_MARKER", None)

        self.assertEqual(load_runtime_env(), env_file)
        self.assertEqual(os.environ["PROCRAFILER_TEST_MARKER"], "set-by-explicit-file")


if __name__ == "__main__":
    unittest.main()
