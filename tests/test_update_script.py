from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UPDATE = _REPO_ROOT / "scripts" / "update.sh"

_STUB_PIP = "#!/usr/bin/env bash\nexit 0\n"
_STUB_PROCRA = '#!/usr/bin/env bash\necho "procrafiler 9.9.9"\n'


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("git") and _UPDATE.is_file(),
    "bash / git / update.sh unavailable",
)
class TestUpdateScript(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._git_env = {
            **os.environ, "HOME": str(self.home),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t",
        }

        # A tagged history (v0.1.0, v0.2.0) pushed to a bare "origin", cloned as the repo.
        work = self.home / "work"
        work.mkdir()
        self._git(work, "init", "-b", "main")
        (work / "f.txt").write_text("v1", encoding="utf-8")
        self._git(work, "add", "-A")
        self._git(work, "commit", "-m", "c1")
        self._git(work, "tag", "v0.1.0")
        (work / "f.txt").write_text("v2", encoding="utf-8")
        self._git(work, "add", "-A")
        self._git(work, "commit", "-m", "c2")
        self._git(work, "tag", "v0.2.0")
        origin = self.home / "origin.git"
        self._git(self.home, "init", "--bare", str(origin))
        self._git(work, "remote", "add", "origin", str(origin))
        self._git(work, "push", "origin", "main", "--tags")

        self.repo = self.home / "repo"
        self._git(self.home, "clone", str(origin), str(self.repo))
        self._git(self.repo, "checkout", "v0.1.0")  # start on the OLD tag

        # A fake installed venv + the install metadata update.sh reads.
        app = self.home / ".local/share/procrafiler/app"
        venv_bin = app / ".venv/bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "pip").write_text(_STUB_PIP, encoding="utf-8")
        (venv_bin / "pip").chmod(0o755)
        (venv_bin / "procrafiler").write_text(_STUB_PROCRA, encoding="utf-8")
        (venv_bin / "procrafiler").chmod(0o755)
        self.meta = app / "install-meta.env"
        self.meta.write_text(
            f"MODE=user\nREPO_ROOT={self.repo}\nVENV_DIR={app / '.venv'}\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], env=self._git_env, capture_output=True, text=True, check=True)

    def _describe(self) -> str:
        out = subprocess.run(["git", "-C", str(self.repo), "describe", "--tags"],
                             env=self._git_env, capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def _update(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)
        return subprocess.run(["bash", str(_UPDATE), "--mode", "user", *args],
                              env=env, capture_output=True, text=True)

    def test_checks_out_the_latest_release_tag(self) -> None:
        self.assertEqual(self._describe(), "v0.1.0")  # precondition
        result = self._update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v0.1.0 -> v0.2.0", result.stdout)
        self.assertEqual(self._describe(), "v0.2.0")  # moved to the latest tag

    def test_refuses_a_dirty_clone_and_does_not_move(self) -> None:
        (self.repo / "uncommitted.txt").write_text("oops", encoding="utf-8")
        result = self._update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to update", result.stderr)
        self.assertEqual(self._describe(), "v0.1.0")  # unchanged

    def test_already_on_latest_is_a_noop_checkout(self) -> None:
        self._git(self.repo, "checkout", "v0.2.0")
        result = self._update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already on the latest release", result.stdout)

    def test_missing_install_metadata_exits_nonzero(self) -> None:
        self.meta.unlink()
        result = self._update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Install metadata not found", result.stderr)

    def test_update_never_touches_user_data(self) -> None:
        library = self.home / "ProcraFiler_Library" / "doc.txt"
        library.parent.mkdir(parents=True)
        library.write_text("my important document", encoding="utf-8")
        self._update()
        self.assertEqual(library.read_text(encoding="utf-8"), "my important document")


if __name__ == "__main__":
    unittest.main()
