# pyright: reportUnknownVariableType=false
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from procrafiler.cli import main
from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    get_deletion_mode,
    get_user_language,
    load_feature_settings,
    set_deletion_mode,
    set_feature_flag,
    set_user_language,
)


class TestDeletionModeSetting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_is_tombstone(self) -> None:
        self.assertEqual(get_deletion_mode(self.paths), "tombstone")

    def test_set_and_persist(self) -> None:
        set_deletion_mode(self.paths, "purge")
        self.assertEqual(get_deletion_mode(self.paths), "purge")
        # A fresh read of the file confirms persistence.
        self.assertEqual(get_deletion_mode(default_runtime_paths()), "purge")

    def test_invalid_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            set_deletion_mode(self.paths, "shred")

    def test_deletion_mode_and_feature_flags_coexist(self) -> None:
        # The two settings share one file; neither must wipe the other.
        set_deletion_mode(self.paths, "purge")
        set_feature_flag(self.paths, "mirror_sync", False)
        self.assertEqual(get_deletion_mode(self.paths), "purge")  # survived the feature write
        self.assertFalse(load_feature_settings(self.paths)["features"]["mirror_sync"])

        set_deletion_mode(self.paths, "tombstone")
        self.assertFalse(load_feature_settings(self.paths)["features"]["mirror_sync"])  # survived the mode write
        self.assertEqual(get_deletion_mode(self.paths), "tombstone")

    def test_user_language_default_set_and_invalid(self) -> None:
        self.assertEqual(get_user_language(self.paths), "en")  # default
        set_user_language(self.paths, "FR")  # normalised to lowercase
        self.assertEqual(get_user_language(self.paths), "fr")
        with self.assertRaises(ValueError):
            set_user_language(self.paths, "français")  # not a code

    def test_language_deletion_mode_and_features_all_coexist(self) -> None:
        set_user_language(self.paths, "fr")
        set_deletion_mode(self.paths, "purge")
        set_feature_flag(self.paths, "mirror_sync", False)
        self.assertEqual(get_user_language(self.paths), "fr")
        self.assertEqual(get_deletion_mode(self.paths), "purge")
        self.assertFalse(load_feature_settings(self.paths)["features"]["mirror_sync"])

    def test_cli_language_shows_and_sets(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["language"]), 0)
        self.assertIn("language: en", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["language", "fr"]), 0)
        self.assertEqual(get_user_language(self.paths), "fr")

    def test_cli_shows_and_sets(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["deletion-mode"]), 0)
        self.assertIn("deletion_mode: tombstone", out.getvalue())

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["deletion-mode", "purge"]), 0)
        self.assertIn("purge", out.getvalue())
        self.assertEqual(get_deletion_mode(self.paths), "purge")


if __name__ == "__main__":
    unittest.main()
