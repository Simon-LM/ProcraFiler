from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UNINSTALL = _REPO_ROOT / "scripts" / "uninstall.sh"


@unittest.skipUnless(shutil.which("bash") and _UNINSTALL.is_file(), "bash / uninstall.sh unavailable")
class TestUninstallScript(unittest.TestCase):
    """The cardinal install/uninstall guarantee: uninstall NEVER deletes the user's library."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        for d in (".local/share/procrafiler/app", ".local/bin", ".config/procrafiler",
                  "ProcraFiler_Library/Personal"):
            (self.home / d).mkdir(parents=True)
        for f in (".local/bin/procrafiler", ".config/procrafiler/procrafiler.env",
                  ".config/procrafiler/settings.json", ".config/procrafiler/policy.toml",
                  ".config/procrafiler/context.md", ".local/share/procrafiler/catalog.db",
                  ".local/share/procrafiler/actions_log.jsonl", ".local/share/procrafiler/search_index.db",
                  ".local/share/procrafiler/catalog_snapshot.json"):
            (self.home / f).write_text("x", encoding="utf-8")
        self.doc = self.home / "ProcraFiler_Library/Personal/mydoc.txt"
        self.doc.write_text("my important document", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> None:
        result = self._try(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def _try(self, *args: str, answers: str = "", **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PROCRAFILER_")}
        env["HOME"] = str(self.home)
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(_UNINSTALL), "--mode", "user", *args],
            env=env, capture_output=True, text=True, input=answers,
        )

    def _exists(self, rel: str) -> bool:
        return (self.home / rel).exists()

    def _copies(self) -> list[Path]:
        """Copies of the context file the uninstaller wrote out, if any."""
        return sorted(self.home.glob("procrafiler-context-*"))

    # --- it must never claim a success it did not verify --------------------

    def test_uninstalling_a_mode_that_was_never_installed_fails_loudly(self) -> None:
        """The defect: `rm -f` and `rm -rf` say nothing about a missing target, and
        the tick was printed regardless. Install with --mode system, uninstall with
        the default (user), and it removed two paths that were never there, declared
        victory, and left /opt/procrafiler/app in place."""
        shutil.rmtree(self.home / ".local/share/procrafiler/app")
        (self.home / ".local/bin/procrafiler").unlink()

        result = self._try()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Nothing was found to remove", result.stderr)
        self.assertIn("--mode system", result.stderr, "it must point at the other mode")

    def test_every_target_is_reported_by_name_and_outcome(self) -> None:
        """"removed" and "already absent" must be distinguishable, per target."""
        result = self._try()
        self.assertIn("removed", result.stdout)
        self.assertIn(".local/share/procrafiler/app", result.stdout)
        self.assertIn("already absent", result.stdout, "the uninstall launcher was never installed here")

    def test_the_uninstall_launcher_is_removed_too(self) -> None:
        launcher = self.home / ".local/bin/procrafiler-uninstall"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        self._run()
        self.assertFalse(launcher.exists())

    # --- a purge must not be redirected by the environment ------------------

    def test_purge_refuses_while_a_root_variable_is_exported(self) -> None:
        """The trap: these redirect the catalog and config elsewhere — a development
        sandbox, for instance — so a purge would erase THAT, not the installation.
        This project's own sandbox/run.sh exports them."""
        elsewhere = self.home / "somewhere-else"
        elsewhere.mkdir()
        result = self._try("--purge", "--yes", PROCRAFILER_HOME=str(elsewhere))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to purge", result.stderr)
        self.assertIn("PROCRAFILER_HOME", result.stderr)
        self.assertTrue(elsewhere.exists(), "it touched the redirected directory anyway")
        self.assertTrue(self._exists(".local/share/procrafiler/catalog.db"), "it purged despite refusing")

    def test_a_plain_uninstall_is_unaffected_by_those_variables(self) -> None:
        """Anti-vacuity: they decide what a PURGE deletes. Removing the app does not
        depend on them, so refusing there would be obstruction, not safety."""
        result = self._try(PROCRAFILER_HOME=str(self.home / "elsewhere"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self._exists(".local/share/procrafiler/app"))

    # --- a purge must actually leave nothing behind -------------------------

    def test_purge_removes_the_state_directory_whole(self) -> None:
        """The old list named files only, so the directory, its lock and the stale
        subdirectories of older versions survived — and it said "purged" anyway."""
        state = self.home / ".local/share/procrafiler"
        (state / "procrafiler.lock").write_text("8", encoding="utf-8")
        for stale in ("history", "index", "logs", "review_queue"):
            (state / stale).mkdir()

        self._run("--purge", "--yes")
        self.assertFalse(state.exists(), f"left behind: {list(state.rglob('*')) if state.exists() else ''}")

    def test_purge_leaves_no_empty_config_directory_when_nothing_is_owed(self) -> None:
        (self.home / ".config/procrafiler/context.md").unlink()
        self._run("--purge", "--yes")
        self.assertFalse(self._exists(".config/procrafiler"))

    def test_default_removes_app_keeps_all_user_data(self) -> None:
        self._run()
        self.assertFalse(self._exists(".local/share/procrafiler/app"))   # app removed
        self.assertFalse(self._exists(".local/bin/procrafiler"))          # launcher removed
        self.assertTrue(self.doc.exists())                                # LIBRARY kept
        self.assertTrue(self._exists(".config/procrafiler/procrafiler.env"))  # config kept
        self.assertTrue(self._exists(".local/share/procrafiler/catalog.db"))  # state kept

    def test_purge_removes_config_and_state_but_never_the_library(self) -> None:
        self._run("--purge", "--yes")
        self.assertFalse(self._exists(".config/procrafiler/procrafiler.env"))   # env purged
        self.assertFalse(self._exists(".config/procrafiler/settings.json"))     # settings purged
        self.assertFalse(self._exists(".local/share/procrafiler/catalog.db"))   # state purged
        self.assertFalse(self._exists(".local/share/procrafiler/search_index.db"))
        self.assertTrue(self.doc.exists())                                      # LIBRARY SACRED

    # --- the context file: personal, so removed, but only on your terms -----

    def test_purge_removes_the_context_file_and_the_now_empty_config_dir(self) -> None:
        """It used to be kept "because it is yours" — and it stayed behind in
        ~/.config, holding who you are and what you do, on a machine you had just
        wiped the app from. A purge that leaves your personal notes is a leak."""
        self._run("--purge", "--yes")
        self.assertFalse(self._exists(".config/procrafiler/context.md"))
        self.assertFalse(self._exists(".config/procrafiler"))

    def test_yes_alone_writes_no_copy_anywhere(self) -> None:
        """Unattended, nobody can be asked — and a copy of somebody's personal notes
        that nobody asked for is the same leak under a new name."""
        result = self._try("--purge", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._copies(), [], "it scattered a copy without being asked")

    def test_keep_context_copies_it_out_and_says_where(self) -> None:
        original = (self.home / ".config/procrafiler/context.md").read_text(encoding="utf-8")
        result = self._try("--purge", "--yes", "--keep-context")

        self.assertEqual(result.returncode, 0, result.stderr)
        copies = self._copies()
        self.assertEqual(len(copies), 1, f"expected exactly one copy, got {copies}")
        self.assertEqual(copies[0].read_text(encoding="utf-8"), original)
        self.assertFalse(self._exists(".config/procrafiler/context.md"), "the original stayed")

        # Twice, on purpose, and each one is asserted here because neither can stand
        # in for the other: once as it happens, among the other removals, and once in
        # the closing summary — the part still on screen when the command is over.
        progress, _, summary = result.stdout.partition("Your documents are safe")
        self.assertIn(str(copies[0]), progress, "the removal report did not name the copy")
        self.assertIn(str(copies[0]), summary, "the closing summary did not name the copy")

    def test_the_copy_is_readable_only_by_its_owner(self) -> None:
        """It says who wrote it. Moving it must not widen who can read it."""
        import stat as stat_mod

        self._run("--purge", "--yes", "--keep-context")
        self.assertEqual(stat_mod.S_IMODE(self._copies()[0].stat().st_mode), 0o600)

    def test_the_copy_is_offered_and_declining_it_is_the_default(self) -> None:
        """Offered, never imposed: answering the purge prompt must not be taken as
        consent to leave a second copy of your personal notes lying about."""
        result = self._try("--purge", answers="y\n\n")   # purge: yes; copy: default

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Keep a copy", result.stdout, "it never offered the copy")
        self.assertEqual(self._copies(), [], "the default answer kept a copy anyway")
        self.assertFalse(self._exists(".config/procrafiler/context.md"))

    def test_answering_yes_to_the_offer_keeps_the_copy(self) -> None:
        """Anti-vacuity for the test above: the offer is real, not decoration."""
        result = self._try("--purge", answers="y\ny\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._copies()), 1)

    def test_drop_context_answers_the_offer_up_front(self) -> None:
        result = self._try("--purge", "--drop-context", answers="y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Keep a copy", result.stdout, "it asked despite being told")
        self.assertEqual(self._copies(), [])
        self.assertFalse(self._exists(".config/procrafiler/context.md"))

    @unittest.skipIf(os.geteuid() == 0, "root can read anything, so the copy cannot fail")
    def test_a_copy_that_cannot_be_written_leaves_the_original_alone(self) -> None:
        """The one outcome that would be unforgivable: asked to keep it, failed to
        keep it, deleted it anyway."""
        context = self.home / ".config/procrafiler/context.md"
        context.chmod(0o000)
        try:
            result = self._try("--purge", "--yes", "--keep-context")
            self.assertEqual(self._copies(), [])
            self.assertTrue(context.exists(), "it deleted the only copy there was")
            self.assertIn("KEPT", result.stderr)
        finally:
            context.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
