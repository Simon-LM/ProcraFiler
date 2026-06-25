from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALL = _REPO_ROOT / "scripts" / "install.sh"

# A stub "python" that only answers `-m venv <dir>` by creating a fake venv with
# no-op pip + procrafiler executables. This lets us test install.sh's file
# management (env seeding, permissions, launcher, meta) WITHOUT a real pip install.
_STUB_PYTHON = """#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  d="$3"
  mkdir -p "$d/bin"
  printf '#!/usr/bin/env bash\\nexit 0\\n' > "$d/bin/pip" && chmod +x "$d/bin/pip"
  printf '#!/usr/bin/env bash\\necho "procrafiler 9.9.9"\\n' > "$d/bin/procrafiler" && chmod +x "$d/bin/procrafiler"
fi
exit 0
"""


@unittest.skipUnless(shutil.which("bash") and _INSTALL.is_file(), "bash / install.sh unavailable")
class TestInstallScript(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.stub = self.home / "stub-python"
        self.stub.write_text(_STUB_PYTHON, encoding="utf-8")
        self.stub.chmod(0o755)
        # paths install.sh derives in user mode
        self.env_file = self.home / ".config/procrafiler/procrafiler.env"
        self.launcher = self.home / ".local/bin/procrafiler"
        self.venv_pip = self.home / ".local/share/procrafiler/app/.venv/bin/pip"
        self.meta = self.home / ".local/share/procrafiler/app/install-meta.env"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)
        return subprocess.run(
            ["bash", str(_INSTALL), *args],
            env=env, capture_output=True, text=True,
        )

    def _install(self) -> subprocess.CompletedProcess[str]:
        return self._run("--mode", "user", "--python", str(self.stub))

    def test_fresh_install_creates_venv_env_launcher_and_meta(self) -> None:
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.venv_pip.exists())   # the (stub) venv was created
        self.assertTrue(self.env_file.exists())
        self.assertTrue(self.launcher.exists())
        self.assertTrue(self.meta.exists())
        # the env file is seeded from the canonical .env.example
        self.assertIn("PROCRAFILER_AI_ANALYSIS_PRIMARY", self.env_file.read_text(encoding="utf-8"))

    def test_env_file_is_created_0600(self) -> None:
        self._install()
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)

    def test_launcher_points_at_the_env_file_and_runs(self) -> None:
        self._install()
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertIn(f'PROCRAFILER_ENV_FILE="{self.env_file}"', self.launcher.read_text(encoding="utf-8"))
        out = subprocess.run([str(self.launcher), "--version"], capture_output=True, text=True)
        self.assertIn("procrafiler 9.9.9", out.stdout)  # execs the (stub) venv binary

    def test_reinstall_leaves_an_existing_env_file_untouched(self) -> None:
        self._install()
        self.env_file.write_text("MISTRAL_API_KEY=my-secret-key\n", encoding="utf-8")
        result = self._install()  # second install
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "MISTRAL_API_KEY=my-secret-key\n")
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)  # perms re-enforced

    def test_invalid_mode_exits_nonzero(self) -> None:
        result = self._run("--mode", "bogus", "--python", str(self.stub))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid --mode", result.stderr)

    def test_unknown_option_exits_nonzero(self) -> None:
        self.assertEqual(self._run("--nope").returncode, 1)

    def test_missing_python_exits_nonzero(self) -> None:
        result = self._run("--mode", "user", "--python", "definitely-not-a-real-python-xyz")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)

    def test_help_exits_zero(self) -> None:
        self.assertEqual(self._run("--help").returncode, 0)


if __name__ == "__main__":
    unittest.main()
