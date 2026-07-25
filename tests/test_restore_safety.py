# pyright: reportUnknownVariableType=false
"""`restore` must never destroy a document — item B of docs/pre-prod-hardening.md.

`restore` is a RECOVERY command that used to be destructive when misused: it
copied straight over existing files with no confirmation, no dry run, and no copy
kept. The tell was the inconsistency — the catalog DB was backed up before being
replaced, the user's documents were not. A user pointing it at a stale mirror
"just to check it works" silently rolled their library back.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.restore import (
    plan_restore,
    replicate_catalog_to_mirror,
    restore_from_mirror,
)

CURRENT = b"CURRENT library version the user cares about"
OLD = b"OLD mirror version"


class TestRestoreSafety(unittest.TestCase):
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
        CatalogRepository(self.paths.catalog_db_file).init_schema()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self) -> None:
        """A stale mirror vs a library holding the user's newer edit."""
        (self.paths.library_root / "Personal").mkdir(parents=True, exist_ok=True)
        (self.paths.mirror_root / "Personal").mkdir(parents=True, exist_ok=True)
        (self.paths.library_root / "Personal" / "doc.txt").write_bytes(CURRENT)
        (self.paths.library_root / "Personal" / "mine.txt").write_bytes(b"only in library")
        (self.paths.mirror_root / "Personal" / "doc.txt").write_bytes(OLD)
        (self.paths.mirror_root / "Personal" / "fresh.txt").write_bytes(b"only in mirror")
        replicate_catalog_to_mirror(self.paths)

    # --- the plan -------------------------------------------------------

    def test_plan_separates_new_overwrite_identical_and_library_only(self) -> None:
        self._seed()
        (self.paths.library_root / "Personal" / "same.txt").write_bytes(b"same bytes")
        (self.paths.mirror_root / "Personal" / "same.txt").write_bytes(b"same bytes")

        plan = plan_restore(self.paths, self.paths.mirror_root)

        self.assertEqual(plan.overwrites, ["Personal/doc.txt"])
        self.assertIn("Personal/fresh.txt", plan.new_files)
        self.assertEqual(plan.identical, ["Personal/same.txt"])
        self.assertIn("Personal/mine.txt", plan.library_only)
        self.assertTrue(plan.destructive)

    def test_a_restore_into_a_fresh_library_is_not_destructive(self) -> None:
        (self.paths.mirror_root / "Personal").mkdir(parents=True, exist_ok=True)
        (self.paths.mirror_root / "Personal" / "doc.txt").write_bytes(OLD)
        replicate_catalog_to_mirror(self.paths)

        plan = plan_restore(self.paths, self.paths.mirror_root)

        self.assertFalse(plan.destructive)
        self.assertEqual(plan.overwrites, [])

    # --- dry run --------------------------------------------------------

    def test_dry_run_changes_absolutely_nothing(self) -> None:
        self._seed()
        before = {
            p: p.read_bytes() for p in self.paths.library_root.rglob("*") if p.is_file()
        }

        report = restore_from_mirror(self.paths, self.paths.mirror_root, dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual(report.files_copied, 0)
        after = {p: p.read_bytes() for p in self.paths.library_root.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "a dry run must not touch the library")
        # And it still reports the danger.
        self.assertIsNotNone(report.plan)
        assert report.plan is not None
        self.assertEqual(report.plan.overwrites, ["Personal/doc.txt"])

    # --- the real thing -------------------------------------------------

    def test_an_overwritten_document_is_recoverable_from_the_trash(self) -> None:
        """The heart of item B: the newer version must survive the restore."""
        self._seed()

        report = restore_from_mirror(self.paths, self.paths.mirror_root)

        # The restore applied…
        self.assertEqual((self.paths.library_root / "Personal" / "doc.txt").read_bytes(), OLD)
        # …but the user's version is intact in the trash, not destroyed.
        rescued = self.paths.library_trash_manual_dir / "Personal" / "doc.txt"
        self.assertTrue(rescued.is_file(), "the overwritten document was destroyed")
        self.assertEqual(rescued.read_bytes(), CURRENT)
        self.assertEqual(report.overwritten_to_trash, 1)

    def test_library_only_documents_are_left_untouched(self) -> None:
        self._seed()
        restore_from_mirror(self.paths, self.paths.mirror_root)
        self.assertEqual(
            (self.paths.library_root / "Personal" / "mine.txt").read_bytes(), b"only in library"
        )

    def test_identical_documents_are_not_sent_to_the_trash(self) -> None:
        """Restoring an unchanged document must not litter the trash."""
        (self.paths.library_root / "Personal").mkdir(parents=True, exist_ok=True)
        (self.paths.mirror_root / "Personal").mkdir(parents=True, exist_ok=True)
        (self.paths.library_root / "Personal" / "doc.txt").write_bytes(b"same")
        (self.paths.mirror_root / "Personal" / "doc.txt").write_bytes(b"same")
        replicate_catalog_to_mirror(self.paths)

        report = restore_from_mirror(self.paths, self.paths.mirror_root)

        self.assertEqual(report.overwritten_to_trash, 0)
        self.assertEqual(
            [p for p in self.paths.library_trash_manual_dir.rglob("*") if p.is_file()], []
        )

    def test_two_successive_restores_keep_both_rescued_versions(self) -> None:
        """A second restore must not clobber the copy the first one rescued."""
        self._seed()
        restore_from_mirror(self.paths, self.paths.mirror_root)
        # The user edits again, then restores from the same stale mirror again.
        (self.paths.library_root / "Personal" / "doc.txt").write_bytes(b"SECOND edit")
        restore_from_mirror(self.paths, self.paths.mirror_root)

        rescued = sorted(
            p.read_bytes()
            for p in (self.paths.library_trash_manual_dir / "Personal").iterdir()
            if p.is_file()
        )
        self.assertEqual(rescued, sorted([CURRENT, b"SECOND edit"]))


if __name__ == "__main__":
    unittest.main()
