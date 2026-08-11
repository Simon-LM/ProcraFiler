# pyright: reportUnknownVariableType=false
"""A development build must not write into anyone's real library.

The incident: a development run created a full layout — inbox, library taxonomy,
mirror, state — in the developer's real home directory. Nothing was lost, because
it was empty, but nothing in the code stopped it either.

These tests pin the three refusals and, just as importantly, the cases that must
**not** be refused: an ordinary `pip install`, a marked sandbox that has filled up
with test documents, and a deliberate override. A guard that fires on a real user
would be worse than no guard, because it would be turned off.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from procrafiler import dev_guard
from procrafiler.config import RuntimePaths, default_runtime_paths, ensure_runtime_layout
from procrafiler.dev_guard import (
    ALLOW_REAL_DATA_ENV,
    ProductionWriteRefused,
    guard_mutation,
    layout_holds_real_work,
    source_checkout_root,
)


def _paths_under(root: Path) -> RuntimePaths:
    """A complete layout rooted at `root`, without touching the environment."""
    env = {
        "PROCRAFILER_WORKSPACE_DIR": str(root / "inbox"),
        "PROCRAFILER_LIBRARY_DIR": str(root / "lib"),
        "PROCRAFILER_LIBRARY_MIRROR_DIR": str(root / "mirror"),
        "PROCRAFILER_HOME": str(root / "state"),
        "PROCRAFILER_CONFIG_HOME": str(root / "config"),
    }
    with patch.dict(os.environ, env):
        return default_runtime_paths()


class TestSourceCheckoutDetection(unittest.TestCase):
    def test_the_test_suite_itself_runs_from_a_checkout(self) -> None:
        """If this ever returns None the whole guard silently disappears."""
        root = source_checkout_root()
        self.assertIsNotNone(root, "the suite must run from a source checkout")
        assert root is not None
        self.assertTrue((root / "pyproject.toml").is_file())
        self.assertTrue((root / "src" / "procrafiler").is_dir())

    def test_an_installed_package_is_not_a_checkout(self) -> None:
        """The discrimination that keeps a normal `pip install` unaffected: the
        installer copies the package into site-packages, development uses -e."""
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site-packages" / "procrafiler"
            site.mkdir(parents=True)
            with patch.object(dev_guard, "__file__", str(site / "dev_guard.py")):
                self.assertIsNone(source_checkout_root())

    def test_a_src_layout_without_pyproject_is_not_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "src" / "procrafiler"
            pkg.mkdir(parents=True)
            with patch.object(dev_guard, "__file__", str(pkg / "dev_guard.py")):
                self.assertIsNone(source_checkout_root())


class TestGuardRefusals(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ.pop(ALLOW_REAL_DATA_ENV, None)

    def tearDown(self) -> None:
        os.environ.pop(ALLOW_REAL_DATA_ENV, None)
        self.tmp.cleanup()

    def _fake_installation(self, library: Path) -> Path:
        """An `install-meta.env` describing an installation whose library is
        `library`, so the guard is tested against a *configured* installation and
        not merely against the built-in defaults."""
        env_file = self.root / "procrafiler.env"
        env_file.write_text(f"PROCRAFILER_LIBRARY_DIR={library}\n", encoding="utf-8")
        meta = self.root / "install-meta.env"
        meta.write_text(
            f"VENV_DIR={self.root}/app/.venv\nENV_FILE={env_file}\n", encoding="utf-8"
        )
        return meta

    # --- guard 3: the built-in defaults ---------------------------------

    def test_the_default_layout_is_refused(self) -> None:
        """This is the exact call that caused the incident.

        Reached now through `force_home_defaults`, because a source checkout no
        longer *defaults* to the home at all — see the test below. The guard is
        unchanged and still refuses; what changed is who arrives at its door.
        """
        home = self.root / "home"
        home.mkdir()
        with patch.object(Path, "home", return_value=home):
            with patch.dict(os.environ, {}, clear=False):
                for name in dev_guard.ROOT_ENV_VARS:
                    os.environ.pop(name, None)
                paths = default_runtime_paths(force_home_defaults=True)
                with self.assertRaises(ProductionWriteRefused) as caught:
                    guard_mutation(paths)
        # A refusal that does not say where it was pointed cannot be acted on.
        self.assertIn(str(paths.library_root), str(caught.exception))
        self.assertIn("PROCRAFILER_ALLOW_REAL_DATA", str(caught.exception))
        # …and it really did not create it.
        self.assertFalse(paths.library_root.exists())

    def test_a_checkout_no_longer_even_names_the_home(self) -> None:
        """The improvement layered on top of the three guards.

        Before: an unconfigured development run computed the real library's path
        and was turned away. Safe, but one guard away from the incident. Now it
        computes its own sandbox, so there is nothing to turn away — and the guards
        stay underneath as the net, as the test above proves.
        """
        home = self.root / "home"
        home.mkdir()
        checkout = dev_guard.source_checkout_root()
        assert checkout is not None, "this suite runs from a checkout"

        with patch.object(Path, "home", return_value=home):
            with patch.dict(os.environ, {}, clear=False):
                for name in dev_guard.ROOT_ENV_VARS:
                    os.environ.pop(name, None)
                paths = default_runtime_paths()

        sandbox = checkout / "sandbox" / "workspace"
        for root in (paths.workspace_root, paths.library_root, paths.mirror_root,
                     paths.state_root, paths.settings_file.parent):
            with self.subTest(root=root):
                self.assertTrue(root.is_relative_to(sandbox), f"{root} escaped the sandbox")
                self.assertFalse(root.is_relative_to(home), f"{root} still points into the home")

    def test_the_checkout_defaults_match_sandbox_run_sh_exactly(self) -> None:
        """Two spellings of "the sandbox" that drifted would give a checkout TWO
        sandboxes — the very collision this removes. The script and the code must
        name the same five directories, so the same test data is seen whether a run
        went through `sandbox/run.sh` or not."""
        checkout = dev_guard.source_checkout_root()
        assert checkout is not None
        script = (checkout / "sandbox" / "run.sh").read_text(encoding="utf-8")
        work = checkout / "sandbox" / "workspace"

        with patch.dict(os.environ, {}, clear=False):
            for name in dev_guard.ROOT_ENV_VARS:
                os.environ.pop(name, None)
            paths = default_runtime_paths()

        expected = {
            'PROCRAFILER_WORKSPACE_DIR="$WORK/ProcraFiler_Inbox"': paths.workspace_root,
            'PROCRAFILER_LIBRARY_DIR="$WORK/ProcraFiler_Library"': paths.library_root,
            'PROCRAFILER_LIBRARY_MIRROR_DIR="$WORK/ProcraFiler_Library_Mirror"': paths.mirror_root,
            'PROCRAFILER_HOME="$WORK/state"': paths.state_root,
            'PROCRAFILER_CONFIG_HOME="$WORK/config"': paths.settings_file.parent,
        }
        for line, computed in expected.items():
            with self.subTest(line=line):
                self.assertIn(line, script, "sandbox/run.sh no longer exports this")
                suffix = line.split('"$WORK/')[1].rstrip('"')
                self.assertEqual(computed, work / suffix)

    # --- guard 1: the installed layout ----------------------------------

    def test_the_installed_layout_is_refused_even_when_it_is_not_the_default(self) -> None:
        """A user who moved their library elsewhere must be protected there, not at
        the path the defaults would have chosen."""
        elsewhere = self.root / "somewhere" / "MyLibrary"
        meta = self._fake_installation(elsewhere)
        paths = _paths_under(self.root / "target")
        paths = RuntimePaths(**{**paths.__dict__, "library_root": elsewhere})

        with patch.object(dev_guard, "install_meta_file", return_value=meta):
            with self.assertRaises(ProductionWriteRefused) as caught:
                guard_mutation(paths)
        self.assertIn("INSTALLED", str(caught.exception))
        self.assertIn(str(elsewhere), str(caught.exception))

    def test_a_layout_unrelated_to_the_installation_is_allowed(self) -> None:
        """Anti-vacuity: the previous test must fail because of the MATCH, not
        because any installation record refuses everything."""
        meta = self._fake_installation(self.root / "somewhere" / "MyLibrary")
        paths = _paths_under(self.root / "sandbox")
        with patch.object(dev_guard, "install_meta_file", return_value=meta):
            guard_mutation(paths)  # must not raise

    # --- guard 2: a library that already holds work ---------------------

    def test_an_unmarked_library_holding_documents_is_refused(self) -> None:
        paths = _paths_under(self.root / "target")
        paths.library_root.mkdir(parents=True)
        (paths.library_root / "someones-invoice.pdf").write_bytes(b"%PDF-")
        with self.assertRaises(ProductionWriteRefused) as caught:
            guard_mutation(paths)
        self.assertIn("already holds documents", str(caught.exception))

    def test_a_non_empty_action_log_also_counts_as_real_work(self) -> None:
        """A library can be empty while the app has clearly been used — a run that
        sent everything to manual review, for instance."""
        paths = _paths_under(self.root / "target")
        paths.state_root.mkdir(parents=True)
        paths.actions_log_file.write_text('{"action":"ingest_detected"}\n', encoding="utf-8")
        with self.assertRaises(ProductionWriteRefused):
            guard_mutation(paths)

    def test_a_marked_sandbox_full_of_documents_is_allowed(self) -> None:
        """The case that makes guard 2 usable at all: a development sandbox fills up
        with test documents exactly like a real library."""
        paths = _paths_under(self.root / "target")
        paths.library_root.mkdir(parents=True)
        (paths.library_root / "test.txt").write_text("x")
        paths.state_root.mkdir(parents=True)
        dev_guard.mark_sandbox(paths)
        guard_mutation(paths)  # must not raise

    def test_an_empty_taxonomy_skeleton_is_not_real_work(self) -> None:
        """`ensure_runtime_layout` creates ~47 empty directories. If those counted,
        the guard would refuse the second run of every sandbox."""
        paths = _paths_under(self.root / "target")
        (paths.library_root / "Personal" / "Administrative" / "Taxes").mkdir(parents=True)
        self.assertFalse(layout_holds_real_work(paths))

    # --- the escape hatches ---------------------------------------------

    def test_the_explicit_override_lets_it_through(self) -> None:
        paths = _paths_under(self.root / "target")
        paths.library_root.mkdir(parents=True)
        (paths.library_root / "doc.pdf").write_bytes(b"%PDF-")
        os.environ[ALLOW_REAL_DATA_ENV] = "1"
        guard_mutation(paths)  # must not raise

    def test_an_ordinary_installation_is_never_guarded(self) -> None:
        """The most important negative: a user who ran `pip install procrafiler`
        must never see any of this."""
        home = self.root / "home"
        home.mkdir()
        with patch.object(dev_guard, "source_checkout_root", return_value=None):
            with patch.object(Path, "home", return_value=home):
                for name in dev_guard.ROOT_ENV_VARS:
                    os.environ.pop(name, None)
                guard_mutation(default_runtime_paths())  # must not raise


class TestGuardIsWiredIntoLayoutCreation(unittest.TestCase):
    """The guard can be perfect and never called."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ensure_runtime_layout_refuses_the_default_layout(self) -> None:
        """The wiring: the guard sits at the one function ~30 entry points call.

        `force_home_defaults` reaches the production layout from a checkout, which
        is what this asserts is refused — a checkout's own default is now its
        sandbox, and that case is covered in `TestGuardRefusals`.
        """
        home = self.root / "home"
        home.mkdir()
        with patch.object(Path, "home", return_value=home):
            for name in dev_guard.ROOT_ENV_VARS:
                os.environ.pop(name, None)
            paths = default_runtime_paths(force_home_defaults=True)
            with self.assertRaises(ProductionWriteRefused):
                ensure_runtime_layout(paths)
        self.assertEqual(
            list(home.iterdir()), [], "the layout was created despite the refusal"
        )

    def test_a_sandbox_is_marked_on_creation(self) -> None:
        """Without this, guard 2 would refuse the sandbox from its second run on."""
        paths = _paths_under(self.root / "sandbox")
        ensure_runtime_layout(paths)
        self.assertTrue(dev_guard.is_marked_sandbox(paths))
        # And the layout stays usable once it holds documents.
        (paths.library_root / "filed.txt").write_text("x")
        ensure_runtime_layout(paths)


if __name__ == "__main__":
    unittest.main()
