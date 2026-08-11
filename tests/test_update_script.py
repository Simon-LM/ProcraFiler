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
        # `git init --bare` points HEAD at `master`; we pushed `main`. Without this a
        # clone comes out with a dangling HEAD and no working tree at all.
        self._git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

        # The user's own clone — the one they ran install.sh from. Nothing in an
        # update may ever touch it again; `test_the_users_own_clone_is_untouched`
        # is the assertion that says so.
        self.repo = self.home / "repo"
        self._git(self.home, "clone", str(origin), str(self.repo))

        # The INSTALLATION's own source clone, which is what an update operates on.
        app = self.home / ".local/share/procrafiler/app"
        self.src = app / "src"
        self._git(self.home, "clone", str(origin), str(self.src))
        self._git(self.src, "checkout", "v0.1.0")  # start on the OLD tag

        # A fake installed venv + the install metadata update.sh reads.
        venv_bin = app / ".venv/bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "pip").write_text(_STUB_PIP, encoding="utf-8")
        (venv_bin / "pip").chmod(0o755)
        (venv_bin / "procrafiler").write_text(_STUB_PROCRA, encoding="utf-8")
        (venv_bin / "procrafiler").chmod(0o755)
        self.meta = app / "install-meta.env"
        self.meta.write_text(
            f"MODE=user\nREPO_ROOT={self.repo}\nSRC_DIR={self.src}\n"
            f"SOURCE_KIND=git\nVENV_DIR={app / '.venv'}\nVERSION=0.0.0\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], env=self._git_env, capture_output=True, text=True, check=True)

    def _describe(self, repo: Path | None = None) -> str:
        out = subprocess.run(["git", "-C", str(repo or self.src), "describe", "--tags"],
                             env=self._git_env, capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def _rev(self, repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", *args],
                              env=self._git_env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def _head(self, repo: Path) -> str:
        return self._rev(repo, "HEAD")

    def _branch(self, repo: Path) -> str:
        return self._rev(repo, "--abbrev-ref", "HEAD")

    def _meta(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in self.meta.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )

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

    def test_the_users_own_clone_is_untouched(self) -> None:
        """The defect this rewrite exists for. An update used to `git checkout` a
        release tag inside whatever directory the install had been run from —
        detaching a developer's HEAD, or failing outright once that folder was gone.
        It now works only inside the installation's own clone."""
        head_before = self._head(self.repo)
        branch_before = self._branch(self.repo)

        self.assertEqual(self._update().returncode, 0)

        self.assertEqual(head_before, self._head(self.repo), "the update moved the user's own HEAD")
        self.assertEqual(branch_before, self._branch(self.repo), "the update detached the user's own clone")

    def test_it_updates_even_after_the_users_clone_is_deleted(self) -> None:
        """Tidying away the folder you installed from is an ordinary thing to do.
        It used to leave the app permanently un-updatable."""
        shutil.rmtree(self.repo)
        result = self._update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._describe(), "v0.2.0")

    def test_refuses_a_dirty_installation_source_and_does_not_move(self) -> None:
        """Nothing is supposed to edit the installation's own tree. Modified means
        something went wrong, and checking a tag out over it would either fail or
        carry those edits into the installed package."""
        (self.src / "uncommitted.txt").write_text("oops", encoding="utf-8")
        result = self._update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to update", result.stderr)
        self.assertIn("--reinstall", result.stderr)
        self.assertEqual(self._describe(), "v0.1.0")  # unchanged

    def test_already_on_latest_is_a_noop_checkout(self) -> None:
        self._git(self.src, "checkout", "v0.2.0")
        result = self._update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already on the latest release", result.stdout)

    def _make_it_an_old_style_installation(self) -> None:
        """Metadata as the previous installer wrote it: the user's own clone, and no
        source of the installation's own."""
        self.meta.write_text(
            f"MODE=user\nREPO_ROOT={self.repo}\nVENV_DIR={self.meta.parent / '.venv'}\n",
            encoding="utf-8",
        )
        shutil.rmtree(self.src)

    def test_an_installation_from_the_older_installer_is_repaired_in_place(self) -> None:
        """Those installations recorded only the user's own clone, and updating used
        to check a tag out inside it. Refusing them outright would leave the people
        who already have the defect with no way forward but a full reinstall — so the
        source is copied OUT of that clone once, and it is never touched again."""
        self._make_it_an_old_style_installation()
        head_before = self._head(self.repo)
        branch_before = self._branch(self.repo)

        result = self._update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("predates the self-contained installer", result.stdout)
        self.assertEqual(self._meta()["SRC_DIR"], str(self.src), "it did not record the new source")
        self.assertEqual(self._meta()["SOURCE_KIND"], "git")
        self.assertEqual(self._describe(), "v0.2.0", "it did not reach the latest release")
        self.assertEqual(head_before, self._head(self.repo), "the repair moved the user's own HEAD")
        self.assertEqual(branch_before, self._branch(self.repo), "the repair detached the user's clone")

    def test_a_repaired_installation_updates_again_without_the_users_clone(self) -> None:
        """Anti-vacuity: the repair is worth nothing if the second update still needs
        the folder it was supposed to free the installation from."""
        self._make_it_an_old_style_installation()
        self.assertEqual(self._update().returncode, 0)
        self._git(self.src, "checkout", "v0.1.0")  # pretend an older release
        shutil.rmtree(self.repo)

        result = self._update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._describe(), "v0.2.0")

    def test_an_old_installation_whose_clone_is_gone_is_sent_to_reinstall(self) -> None:
        """Nothing left to copy from: say so, and name the one command that fixes it."""
        self._make_it_an_old_style_installation()
        shutil.rmtree(self.repo)

        result = self._update()

        self.assertEqual(result.returncode, 1)
        self.assertIn("predates the self-contained installer", result.stderr)
        self.assertIn(str(self.repo), result.stderr, "it must name the clone it looked for")
        self.assertIn("--reinstall", result.stderr)

    def test_the_recorded_version_is_refreshed(self) -> None:
        """A stale VERSION is worse than none: install.sh and uninstall.sh report it
        as fact."""
        self.assertEqual(self._meta()["VERSION"], "0.0.0")  # precondition
        self._update()
        meta = self._meta()
        self.assertEqual(meta["VERSION"], "9.9.9")
        self.assertEqual(meta["SOURCE_REF"], "v0.2.0")
        self.assertRegex(meta["COMMIT"], r"^[0-9a-f]{40}$")

    def test_the_metadata_stays_unreadable_to_others(self) -> None:
        """It names the env file, which holds an API key. Rewriting it must not
        widen its permissions."""
        import stat as stat_mod

        self._update()
        self.assertEqual(stat_mod.S_IMODE(self.meta.stat().st_mode), 0o600)

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
