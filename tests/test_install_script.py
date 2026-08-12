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

    def _run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)
        # Were the suite itself run under sudo, install.sh would resolve the user
        # installation into the real invoking user's home instead of this fake one.
        env.pop("SUDO_USER", None)
        env.update(extra_env)
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
        """Reinstalling must never cost someone their API key. `--reinstall` is now
        required to get here at all — see the refusal tests below."""
        self._install()
        self.env_file.write_text("MISTRAL_API_KEY=my-secret-key\n", encoding="utf-8")
        result = self._run("--mode", "user", "--python", str(self.stub), "--reinstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "MISTRAL_API_KEY=my-secret-key\n")
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)  # perms re-enforced

    # --- one installation, and no accidental second one ---------------------

    def test_a_second_install_is_refused(self) -> None:
        """Silently installing over a live installation is how a working setup
        acquires a half-replaced venv and a source tree from another version."""
        self._install()
        result = self._install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("already installed", result.stderr)

    def test_the_refusal_says_what_is_there_and_how_to_proceed(self) -> None:
        """A refusal that names neither what it found nor the way forward is a wall.
        The version and the revision are what tell someone whether they even want to
        replace it."""
        self._install()
        stderr = self._install().stderr
        self.assertIn("9.9.9", stderr, "the installed version")
        self.assertIn(str(self.meta.parent), stderr, "where it is")
        self.assertIn("update.sh", stderr)
        self.assertIn("--reinstall", stderr)

    def test_the_refusal_changes_nothing(self) -> None:
        """It must not half-replace what it declined to replace."""
        self._install()
        before = self.meta.read_text(encoding="utf-8")
        self.env_file.write_text("MISTRAL_API_KEY=k\n", encoding="utf-8")
        self._install()
        self.assertEqual(self.meta.read_text(encoding="utf-8"), before)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "MISTRAL_API_KEY=k\n")

    # --- the installation owns its source -----------------------------------

    def test_it_clones_the_source_into_the_installation(self) -> None:
        """The clone you install from is read once and never written to again. This
        is what lets someone delete their download folder afterwards."""
        self._install()
        src = self.home / ".local/share/procrafiler/app/src"
        self.assertTrue((src / ".git").exists(), "the installation has no source of its own")
        self.assertTrue((src / "pyproject.toml").is_file())

    def test_the_installed_source_sits_on_a_release_tag(self) -> None:
        """Never a branch head: the version is derived from the tag by
        setuptools-scm, so an installation always names a published release."""
        self._install()
        src = self.home / ".local/share/procrafiler/app/src"
        described = subprocess.run(
            ["git", "-C", str(src), "describe", "--tags"], capture_output=True, text=True,
        ).stdout.strip()
        self.assertRegex(described, r"^v\d+\.\d+\.\d+$", f"not on a release tag: {described!r}")

    def test_the_source_clone_points_at_the_upstream_not_at_the_local_one(self) -> None:
        """So updates keep working after the clone it was installed from is gone."""
        self._install()
        src = self.home / ".local/share/procrafiler/app/src"
        origin = subprocess.run(
            ["git", "-C", str(src), "remote", "get-url", "origin"], capture_output=True, text=True,
        ).stdout.strip()
        upstream = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "remote", "get-url", "origin"], capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(origin, upstream)

    def test_the_developers_clone_is_never_checked_out(self) -> None:
        """The defect that started this: an install recorded the path of a working
        tree, and the update then moved its HEAD onto a tag. Nothing here may touch
        the source repository's state."""
        head_before = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
        ).stdout.strip()
        branch_before = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        self._install()
        head_after = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
        ).stdout.strip()
        branch_after = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head_before, head_after, "the install moved the source repository's HEAD")
        self.assertEqual(branch_before, branch_after, "the install detached the source repository")

    # --- the uninstaller travels with the installation ----------------------

    def test_the_uninstaller_is_copied_in_beside_the_app(self) -> None:
        """It used to live only in the clone. Delete the clone and there was no way
        left to remove the app, nor anything naming the directories it created."""
        self._install()
        copied = self.home / ".local/share/procrafiler/app/uninstall.sh"
        self.assertTrue(copied.is_file())
        self.assertTrue(os.access(copied, os.X_OK))

    def test_an_uninstall_launcher_is_installed_beside_the_app_launcher(self) -> None:
        self._install()
        launcher = self.home / ".local/bin/procrafiler-uninstall"
        self.assertTrue(os.access(launcher, os.X_OK))
        text = launcher.read_text(encoding="utf-8")
        self.assertIn(str(self.home / ".local/share/procrafiler/app/uninstall.sh"), text)
        self.assertIn('--mode "user"', text, "it must uninstall the mode it was installed as")

    # --- what the installation records about itself -------------------------

    def test_the_metadata_records_the_version_and_the_commit(self) -> None:
        """Without them nothing can say what is installed without running it, and no
        script can check what it is acting on."""
        self._install()
        meta = dict(
            line.split("=", 1) for line in self.meta.read_text(encoding="utf-8").splitlines() if "=" in line
        )
        self.assertEqual(meta["VERSION"], "9.9.9")
        self.assertRegex(meta["COMMIT"], r"^[0-9a-f]{40}$")
        self.assertRegex(meta["SOURCE_REF"], r"^v\d+\.\d+\.\d+$")
        self.assertEqual(meta["SOURCE_KIND"], "git")
        self.assertEqual(meta["SRC_DIR"], str(self.home / ".local/share/procrafiler/app/src"))

    def test_the_metadata_is_not_world_readable(self) -> None:
        """It names the env file, which holds an API key."""
        self._install()
        self.assertEqual(stat.S_IMODE(self.meta.stat().st_mode), 0o600)

    # --- install then uninstall, using only what the installation owns -------

    def test_the_copied_uninstaller_removes_the_installation(self) -> None:
        """End to end, and the point of the whole change: nothing here reads the
        repository. The uninstaller invoked is the copy inside the installation.

        Note what this test cannot assert. The copy comes from the RELEASE TAG that
        was installed, not from the working tree — so on a checkout whose fixes are
        not yet released, this exercises the previous release's uninstaller. That is
        correct and deliberate: an installation must be removable by the uninstaller
        of the version it actually runs. The current script's own behaviour is
        covered directly in `test_uninstall_script.py`.
        """
        self._install()
        app = self.home / ".local/share/procrafiler/app"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)

        result = subprocess.run(
            [str(self.home / ".local/bin/procrafiler-uninstall")],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(app.exists(), "the app survived its own uninstaller")
        self.assertFalse((self.home / ".local/bin/procrafiler").exists())
        # Without --purge the configuration stays, API key and all.
        self.assertTrue(self.env_file.exists())

    def test_the_installed_uninstaller_is_the_one_from_the_installed_release(self) -> None:
        """Version-locked on purpose. Removing an installation with a NEWER
        uninstaller than the code it removes is how a script deletes paths that
        version never created, or misses ones it did."""
        self._install()
        src_copy = (self.home / ".local/share/procrafiler/app/src/scripts/uninstall.sh").read_text(
            encoding="utf-8"
        )
        installed = (self.home / ".local/share/procrafiler/app/uninstall.sh").read_text(encoding="utf-8")
        self.assertEqual(installed, src_copy, "the uninstaller does not match the installed source")

    def test_a_library_beside_it_is_untouched_end_to_end(self) -> None:
        """The cardinal guarantee, asserted through the real installed uninstaller
        rather than through the repository's copy of it."""
        library = self.home / "ProcraFiler_Library" / "Personal"
        library.mkdir(parents=True)
        doc = library / "mydoc.txt"
        doc.write_text("my important document", encoding="utf-8")

        self._install()
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)
        subprocess.run(
            [str(self.home / ".local/bin/procrafiler-uninstall"), "--purge", "--yes"],
            env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(doc.read_text(encoding="utf-8"), "my important document")

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


@unittest.skipUnless(shutil.which("bash") and _INSTALL.is_file(), "bash / install.sh unavailable")
class TestOneInstallationPerMachine(unittest.TestCase):
    """One installation per machine, not one per mode.

    The two modes put the CODE in different places — `~/.local/share/procrafiler`
    and `/opt/procrafiler` — but the catalog is derived from `Path.home()`, from WHO
    runs the command rather than from where the code lives. So two installations of
    different versions share one state, and the older binary writes into what the
    newer one keeps. The existing-installation check only looked where the mode
    being installed writes, so each mode was invisible to the other.

    System paths are absolute by nature, so these tests give install.sh a fake root
    (`PROCRAFILER_TEST_ROOT`) rather than writing to the real /opt and /etc.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.fake_root = Path(self.tmp.name) / "fakeroot"
        self.stub = Path(self.tmp.name) / "stub-python"
        self.stub.write_text(_STUB_PYTHON, encoding="utf-8")
        self.stub.chmod(0o755)

        self.system_app = self.fake_root / "opt/procrafiler/app"
        self.system_env = self.fake_root / "etc/procrafiler/procrafiler.env"
        self.system_bin = self.fake_root / "usr/local/bin"
        self.user_app = self.home / ".local/share/procrafiler/app"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)
        env.pop("SUDO_USER", None)
        env["PROCRAFILER_TEST_ROOT"] = str(self.fake_root)
        return subprocess.run(["bash", str(_INSTALL), *args],
                              env=env, capture_output=True, text=True)

    def _install_user(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self._run("--mode", "user", "--python", str(self.stub), *extra)

    def _install_system(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self._run("--mode", "system", "--python", str(self.stub),
                         "--prefix", str(self.fake_root / "usr/local"), *extra)

    def _fake_system_installation(self) -> None:
        # The version and the revision are deliberately unalike. Written as 0.9.0
        # and v0.9.0, either line satisfies a search for the other, and dropping one
        # of them from the refusal would go unnoticed.
        self.system_app.mkdir(parents=True)
        (self.system_app / "install-meta.env").write_text(
            "MODE=system\nVERSION=0.9.0\nSOURCE_REF=abc1234\n", encoding="utf-8")

    # --- neither mode may be installed beside the other ---------------------

    def test_a_user_install_is_refused_when_a_system_one_exists(self) -> None:
        self._fake_system_installation()

        result = self._install_user()

        self.assertEqual(result.returncode, 1)
        self.assertIn("in the other mode", result.stderr)
        self.assertIn("0.9.0", result.stderr, "it must say which version is already there")
        self.assertIn("abc1234", result.stderr, "it must say which revision is already there")
        self.assertIn("--mode system", result.stderr, "it must name how to remove it")
        self.assertFalse(self.user_app.exists(), "it installed anyway")

    def test_a_system_install_is_refused_when_a_user_one_exists(self) -> None:
        self.assertEqual(self._install_user().returncode, 0)

        result = self._install_system()

        self.assertEqual(result.returncode, 1)
        self.assertIn("in the other mode", result.stderr)
        self.assertIn("--mode user", result.stderr)
        self.assertFalse(self.system_app.exists(), "it installed anyway")

    def test_reinstall_does_not_authorise_a_second_installation(self) -> None:
        """`--reinstall` means "replace this installation". Putting a second one
        beside an existing one is not that, so it must not be a way through."""
        self._fake_system_installation()

        result = self._install_user("--reinstall")

        self.assertEqual(result.returncode, 1)
        self.assertIn("in the other mode", result.stderr)
        self.assertFalse(self.user_app.exists())

    def test_a_lone_install_is_unaffected(self) -> None:
        """Anti-vacuity: with nothing in the other mode, both must install."""
        self.assertEqual(self._install_user().returncode, 0)
        shutil.rmtree(self.user_app.parent)
        self.assertEqual(self._install_system().returncode, 0, "system mode became uninstallable")

    # --- system mode must not point every account at one person's library ---

    def test_the_system_launcher_forces_no_env_file_on_anybody(self) -> None:
        """The leak between users. `/etc/procrafiler/procrafiler.env` is one file for
        the whole machine, and `setup` writes PROCRAFILER_LIBRARY_DIR into it as an
        absolute path — so forcing it pointed every other account's inbox and library
        at the home of whoever ran `setup` first."""
        self.assertEqual(self._install_system().returncode, 0)

        launcher = (self.system_bin / "procrafiler").read_text(encoding="utf-8")
        self.assertNotIn("PROCRAFILER_ENV_FILE", launcher)
        self.assertIn("/opt/procrafiler/app/.venv/bin/procrafiler", launcher, "it must still run the app")

    def test_the_user_launcher_still_names_its_own_env_file(self) -> None:
        """Anti-vacuity: in user mode that file belongs to the only person who can
        run the launcher, so naming it is help, not a leak."""
        self.assertEqual(self._install_user().returncode, 0)

        launcher = (self.home / ".local/bin/procrafiler").read_text(encoding="utf-8")
        self.assertIn(f'PROCRAFILER_ENV_FILE="{self.home}/.config/procrafiler/procrafiler.env"', launcher)

    def test_system_mode_still_seeds_a_machine_wide_env_file(self) -> None:
        """It stops being an override and becomes what the app already treats it as:
        the last candidate, used by an account that has no env file of its own."""
        self.assertEqual(self._install_system().returncode, 0)
        self.assertTrue(self.system_env.exists())


if __name__ == "__main__":
    unittest.main()
