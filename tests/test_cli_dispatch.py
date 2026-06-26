"""P3 — CLI dispatch for the remaining thinly-covered commands: `features`,
`feature-set` and `status` (including the durability / backup-reminder lines).
Driven end-to-end through `main([...])`, offline, no AI.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from procrafiler.cli import main
from procrafiler.config import default_runtime_paths, ensure_runtime_layout, load_feature_settings


class _CliEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._snapshot = {k: v for k, v in os.environ.items() if k.startswith("PROCRAFILER_")}
        for k in list(os.environ):
            if k.startswith("PROCRAFILER_"):
                del os.environ[k]
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(tmp / "Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(tmp / "Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(tmp / "Mirror")
        os.environ["PROCRAFILER_HOME"] = str(tmp / "state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(tmp / "config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_")]:
            del os.environ[k]
        os.environ.update(self._snapshot)
        self._tmp.cleanup()

    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()


class TestFeaturesCli(_CliEnv):
    def test_features_lists_flags_returns_0(self) -> None:
        code, out = self._run(["features"])
        self.assertEqual(code, 0)
        self.assertIn("mirror_sync", out)


class TestFeatureSetCli(_CliEnv):
    def test_toggle_off_then_on_persists(self) -> None:
        code, _ = self._run(["feature-set", "mirror_sync", "off"])
        self.assertEqual(code, 0)
        self.assertFalse(load_feature_settings(self.paths)["features"]["mirror_sync"])

        code, _ = self._run(["feature-set", "mirror_sync", "on"])
        self.assertEqual(code, 0)
        self.assertTrue(load_feature_settings(self.paths)["features"]["mirror_sync"])

    def test_unknown_feature_is_rejected_by_argparse(self) -> None:
        # choices=FEATURE_NAMES → argparse errors out (exit code 2), never reaching
        # the handler. Guards against silently accepting a typo'd flag name.
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self._run(["feature-set", "not_a_feature", "on"])


class TestStatusCli(_CliEnv):
    def test_status_shows_durability_section_returns_0(self) -> None:
        code, out = self._run(["status"])
        self.assertEqual(code, 0)
        # The sections scripts/users read, including the durability line.
        self.assertIn("Features", out)
        self.assertIn("mirror_sync", out)
        self.assertIn("Durability", out)
        self.assertIn("last_offline_backup", out)
        self.assertIn("never", out)  # a fresh workspace has no backup yet


if __name__ == "__main__":
    unittest.main()
