from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

import procrafiler.user_setup as us
from procrafiler.config import default_runtime_paths, load_feature_settings
from procrafiler.doctor import STATUS_FAIL, STATUS_SKIP, check_paths
from procrafiler.user_setup import (
    apply_setup,
    collect_paths,
    default_setup_paths,
    on_same_disk,
    update_env_file,
)


def _scripted(answers: list[str]):
    """An `ask` returning the scripted answers in order, ignoring the prompt."""
    it = iter(answers)
    return lambda _prompt: next(it)


def _sink():
    lines: list[str] = []
    return lines, lines.append


class _EnvIsolated(unittest.TestCase):
    """Isolate every PROCRAFILER_* var into a temp dir; restore on teardown."""

    def setUp(self) -> None:
        self._snapshot = {k: v for k, v in os.environ.items() if k.startswith("PROCRAFILER_")}
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        for k in list(os.environ):
            if k.startswith("PROCRAFILER_"):
                del os.environ[k]
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(self.tmp / ".config")
        os.environ["PROCRAFILER_HOME"] = str(self.tmp / ".state")
        os.environ["PROCRAFILER_ENV_FILE"] = str(self.tmp / ".config" / "procrafiler.env")
        # Safe defaults so default_runtime_paths() never points at the real home.
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(self.tmp / "default_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(self.tmp / "default_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(self.tmp / "default_Mirror")

    def tearDown(self) -> None:
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_")]:
            del os.environ[k]
        os.environ.update(self._snapshot)
        self._tmp.cleanup()


class TestUpdateEnvFile(_EnvIsolated):
    def test_new_file_is_0600_with_the_key(self) -> None:
        env = self.tmp / "new.env"
        update_env_file(env, {"PROCRAFILER_LIBRARY_DIR": "/data/Lib"})
        self.assertTrue(env.exists())
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertIn("PROCRAFILER_LIBRARY_DIR=/data/Lib", env.read_text())

    def test_replaces_commented_template_in_place_and_preserves_key(self) -> None:
        env = self.tmp / "e.env"
        env.write_text(
            "# PROCRAFILER_WORKSPACE_DIR=/home/you/Downloads/ProcraFiler_Inbox\n"
            "PROCRAFILER_AI_ANALYSIS_PRIMARY=mistral:mistral-small-latest\n"
            "MISTRAL_API_KEY=secret\n",
            encoding="utf-8",
        )
        update_env_file(env, {"PROCRAFILER_WORKSPACE_DIR": "/data/Inbox"})
        text = env.read_text()
        self.assertIn("PROCRAFILER_WORKSPACE_DIR=/data/Inbox", text)
        self.assertNotIn("# PROCRAFILER_WORKSPACE_DIR", text)  # template replaced
        self.assertEqual(text.count("PROCRAFILER_WORKSPACE_DIR="), 1)  # not duplicated
        self.assertIn("MISTRAL_API_KEY=secret", text)  # key preserved
        self.assertIn("PROCRAFILER_AI_ANALYSIS_PRIMARY=mistral:mistral-small-latest", text)

    def test_unset_drops_active_line_keeps_other_lines(self) -> None:
        env = self.tmp / "e.env"
        env.write_text(
            "PROCRAFILER_LIBRARY_MIRROR_DIR=/old/Mirror\nMISTRAL_API_KEY=secret\n",
            encoding="utf-8",
        )
        update_env_file(
            env,
            {"PROCRAFILER_LIBRARY_DIR": "/data/Lib"},
            unset_keys={"PROCRAFILER_LIBRARY_MIRROR_DIR"},
        )
        text = env.read_text()
        active_mirror = [
            ln for ln in text.splitlines() if ln.strip().startswith("PROCRAFILER_LIBRARY_MIRROR_DIR=")
        ]
        self.assertEqual(active_mirror, [])  # no active mirror line
        self.assertIn("PROCRAFILER_LIBRARY_DIR=/data/Lib", text)
        self.assertIn("MISTRAL_API_KEY=secret", text)


class TestCollectPaths(_EnvIsolated):
    def test_mirror_accepted(self) -> None:
        ask = _scripted(["/data/Inbox", "/data/Lib", "o", "/data/Mir"])
        _, out = _sink()
        choices = collect_paths(ask, out)
        self.assertEqual(choices["inbox"], Path("/data/Inbox"))
        self.assertEqual(choices["library"], Path("/data/Lib"))
        self.assertEqual(choices["mirror"], Path("/data/Mir"))

    def test_mirror_declined(self) -> None:
        ask = _scripted(["/data/Inbox", "/data/Lib", "n"])
        _, out = _sink()
        choices = collect_paths(ask, out)
        self.assertIsNone(choices["mirror"])

    def test_empty_input_uses_defaults(self) -> None:
        ask = _scripted(["", "", "", ""])  # inbox, library, mirror=yes (default), mirror path
        _, out = _sink()
        choices = collect_paths(ask, out)
        d = default_setup_paths()
        self.assertEqual(choices["inbox"], d["inbox"])
        self.assertEqual(choices["library"], d["library"])
        self.assertEqual(choices["mirror"], d["mirror"])


class TestApplySetup(_EnvIsolated):
    def test_mirror_chosen_creates_all_and_flag_on(self) -> None:
        inbox, library, mirror = self.tmp / "Inbox", self.tmp / "Lib", self.tmp / "Mir"
        _, out = _sink()
        env_path = apply_setup({"inbox": inbox, "library": library, "mirror": mirror}, out=out)
        text = env_path.read_text()
        self.assertIn(f"PROCRAFILER_WORKSPACE_DIR={inbox}", text)
        self.assertIn(f"PROCRAFILER_LIBRARY_DIR={library}", text)
        self.assertIn(f"PROCRAFILER_LIBRARY_MIRROR_DIR={mirror}", text)
        self.assertTrue((inbox / "Inbox").exists())
        self.assertTrue(library.exists())
        self.assertTrue(mirror.exists())
        self.assertTrue(load_feature_settings(default_runtime_paths())["features"]["mirror_sync"])

    def test_mirror_declined_skips_folder_and_flag_off(self) -> None:
        inbox, library = self.tmp / "Inbox", self.tmp / "Lib"
        _, out = _sink()
        env_path = apply_setup({"inbox": inbox, "library": library, "mirror": None}, out=out)
        active_mirror = [
            ln for ln in env_path.read_text().splitlines()
            if ln.strip().startswith("PROCRAFILER_LIBRARY_MIRROR_DIR=")
        ]
        self.assertEqual(active_mirror, [])
        paths = default_runtime_paths()
        self.assertFalse(load_feature_settings(paths)["features"]["mirror_sync"])
        self.assertFalse(paths.mirror_root.exists())  # mirror NOT created
        self.assertTrue(library.exists())


class TestSetupFlow(_EnvIsolated):
    def test_setup_runs_writes_and_flows_into_context(self) -> None:
        inbox, library = self.tmp / "In", self.tmp / "Lb"
        called = {}

        def fake_context(*, ask, out):  # noqa: ANN001
            called["ctx"] = True
            return None

        original = us.setup_context
        us.setup_context = fake_context
        try:
            ask = _scripted([str(inbox), str(library), "n", "o"])  # inbox, lib, mirror=no, confirm=yes
            _, out = _sink()
            rc = us.setup(ask=ask, out=out)
        finally:
            us.setup_context = original
        self.assertEqual(rc, 0)
        self.assertTrue(called.get("ctx"))
        self.assertTrue(library.exists())

    def test_setup_aborts_on_decline_creates_nothing(self) -> None:
        library = self.tmp / "Lb"
        ask = _scripted([str(self.tmp / "In"), str(library), "n", "n"])  # confirm=no
        _, out = _sink()
        rc = us.setup(ask=ask, out=out)
        self.assertEqual(rc, 1)
        self.assertFalse(library.exists())

    def test_eof_during_context_keeps_paths_and_returns_0(self) -> None:
        # Paths are applied, then the context questionnaire hits end-of-input
        # (e.g. piped stdin or Ctrl-D): the run must not crash, and the saved
        # paths must survive.
        inbox, library = self.tmp / "In", self.tmp / "Lb"
        answers = iter([str(inbox), str(library), "n", "o"])  # paths + no mirror + confirm

        def ask(_prompt: str) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        _, out = _sink()
        rc = us.setup(ask=ask, out=out)
        self.assertEqual(rc, 0)
        self.assertTrue(library.exists())


class TestMirrorDiskAdvice(_EnvIsolated):
    def test_on_same_disk_true_for_paths_in_one_tree(self) -> None:
        # Two not-yet-existing paths under the same temp dir resolve to the same
        # device via their nearest existing ancestor.
        self.assertTrue(on_same_disk(self.tmp / "Lib", self.tmp / "Mir"))

    def test_setup_warns_when_mirror_on_same_disk(self) -> None:
        inbox, library, mirror = self.tmp / "In", self.tmp / "Lb", self.tmp / "Mir"
        ask = _scripted([str(inbox), str(library), "o", str(mirror), "n"])  # mirror yes, confirm no
        lines, out = _sink()
        rc = us.setup(ask=ask, out=out)
        self.assertEqual(rc, 1)  # declined at confirm → nothing created
        self.assertTrue(any("SAME disk" in ln for ln in lines))
        self.assertFalse(library.exists())


class TestDoctorMirrorOptional(_EnvIsolated):
    def test_missing_mirror_skipped_when_disabled_but_failed_when_enabled(self) -> None:
        paths = default_runtime_paths()  # mirror dir does not exist
        disabled = {c.name: c for c in check_paths(paths, mirror_enabled=False)}
        self.assertEqual(disabled["mirror_root"].status, STATUS_SKIP)
        enabled = {c.name: c for c in check_paths(paths, mirror_enabled=True)}
        self.assertEqual(enabled["mirror_root"].status, STATUS_FAIL)


if __name__ == "__main__":
    unittest.main()
