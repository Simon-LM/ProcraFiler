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

from procrafiler.catalog import CatalogRepository
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

    def test_status_reports_the_integrity_check_too(self) -> None:
        """It used to watch only the backup, so a user who never typed `scrub` never
        verified anything — and a corruption surfaced the day a restore failed."""
        code, out = self._run(["status"])
        self.assertEqual(code, 0)
        self.assertIn("integrity_check", out)
        self.assertIn("no documents filed yet", out, "a fresh workspace has nothing to verify")

    def test_status_nudges_when_documents_have_gone_unchecked(self) -> None:
        catalog = CatalogRepository(self.paths.catalog_db_file)
        catalog.init_schema()
        catalog.upsert_document(
            doc_id="d1", sha256="a" * 64, current_filename="old.txt",
            current_path=str(self.paths.library_root / "old.txt"),
            status="LIBRARY_STORED", updated_at_utc="2020-01-01T00:00:00Z",
        )

        code, out = self._run(["status"])

        self.assertEqual(code, 0)
        self.assertIn("1 of 1 document(s) unverified", out)
        self.assertIn("procrafiler scrub", out, "the nudge must name the command that fixes it")

    def test_a_fresh_install_is_not_reported_as_unreadable(self) -> None:
        """`ensure_runtime_layout` touches catalog.db into existence before any schema
        is written, so a healthy new install has a file holding no tables at all.
        Reporting that as "unreadable (no such table: documents)" would alarm every
        first-time user with a fault they do not have."""
        self.assertEqual(self.paths.catalog_db_file.stat().st_size, 0)  # precondition

        code, out = self._run(["status"])

        self.assertEqual(code, 0)
        self.assertNotIn("unreadable", out)
        self.assertIn("no documents filed yet", out)

    def test_status_creates_nothing(self) -> None:
        """It documents itself as read-only, and it was not: language auto-detection
        consulted the catalog unconditionally, and `sqlite3.connect` CREATES the file
        it opens. `status` and `doctor` have to stay usable exactly when a guard has
        refused a run, which is when writing anything is the wrong move."""
        db = self.paths.catalog_db_file
        db.unlink()

        code, out = self._run(["status"])

        self.assertEqual(code, 0)
        self.assertFalse(db.exists(), "status created the catalog it only meant to read")
        self.assertIn("no catalog yet", out)
        self.assertIn("language: en", out, "it must still answer, with the documented default")


if __name__ == "__main__":
    unittest.main()
