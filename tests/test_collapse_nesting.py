from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from procrafiler.collapse_nesting import collapse_double_nestings, find_double_nestings


class TestCollapseNesting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _touch(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        return p

    def test_collapses_one_redundant_level(self) -> None:
        # The run-14 case: Education/OpenClassrooms/OpenClassrooms/2025/cert.png
        self._touch("Education", "OpenClassrooms", "OpenClassrooms", "2025", "cert.png")
        report = collapse_double_nestings(self.root, apply=True)
        self.assertTrue((self.root / "Education/OpenClassrooms/2025/cert.png").is_file())
        self.assertFalse((self.root / "Education/OpenClassrooms/OpenClassrooms").exists())
        self.assertEqual(report.conflicts, [])

    def test_same_name_in_different_places_is_left_alone(self) -> None:
        # Misc under Personal AND under Work is normal — never touched.
        self._touch("Personal", "Misc", "a.txt")
        self._touch("Work", "Misc", "b.txt")
        self._touch("Personal", "Administrative", "Factures", "c.txt")
        self.assertEqual(find_double_nestings(self.root), [])
        collapse_double_nestings(self.root, apply=True)
        self.assertTrue((self.root / "Personal/Misc/a.txt").is_file())
        self.assertTrue((self.root / "Work/Misc/b.txt").is_file())

    def test_triple_nesting_collapses_inside_out(self) -> None:
        self._touch("X", "X", "X", "deep.txt")
        collapse_double_nestings(self.root, apply=True)
        self.assertTrue((self.root / "X/deep.txt").is_file())
        self.assertFalse((self.root / "X/X").exists())

    def test_dry_run_moves_nothing(self) -> None:
        self._touch("A", "OC", "OC", "f.txt")
        report = collapse_double_nestings(self.root, apply=False)
        self.assertTrue((self.root / "A/OC/OC/f.txt").is_file())  # untouched
        self.assertEqual(len(report.moves), 1)
        src, dst = report.moves[0]
        self.assertEqual(dst, self.root / "A/OC/f.txt")

    def test_file_collision_is_reported_not_overwritten(self) -> None:
        self._touch("OC", "OC", "f.txt")          # inner file
        keep = self._touch("OC", "f.txt")          # already exists in parent
        keep.write_text("ORIGINAL", encoding="utf-8")
        report = collapse_double_nestings(self.root, apply=True)
        self.assertEqual((self.root / "OC/f.txt").read_text(encoding="utf-8"), "ORIGINAL")
        self.assertEqual(len(report.conflicts), 1)


class TestDirectoryMerge(unittest.TestCase):
    """When the inner folder holds a SUBFOLDER whose name already exists in the
    parent, the two must be merged — not left, not clobbered.

    This branch moves real documents and had no test. The collision test above
    only covers a **file** landing on an existing file; a *directory* landing on
    an existing directory takes a different path entirely (recursive merge, then
    remove the emptied source).
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, *parts: str, body: str = "x") -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def test_two_folders_of_the_same_name_are_merged_keeping_both_documents(self) -> None:
        # OC/OC/2025/inner.txt collapsing into an OC/2025/ that already exists.
        self._write("OC", "OC", "2025", "inner.txt", body="INNER")
        self._write("OC", "2025", "outer.txt", body="OUTER")

        report = collapse_double_nestings(self.root, apply=True)

        self.assertEqual((self.root / "OC/2025/inner.txt").read_text(encoding="utf-8"), "INNER")
        self.assertEqual((self.root / "OC/2025/outer.txt").read_text(encoding="utf-8"), "OUTER")
        self.assertFalse((self.root / "OC/OC").exists(), "the emptied inner folder was left behind")
        self.assertEqual(report.conflicts, [], "a merge was reported as a conflict")

    def test_the_merge_recurses_and_still_refuses_to_clobber_a_file(self) -> None:
        """Two levels of folder collision, with one real file collision at the
        bottom: everything merges except that file, which is reported."""
        self._write("OC", "OC", "2025", "Q1", "moved.txt", body="MOVED")
        self._write("OC", "OC", "2025", "Q1", "clash.txt", body="FROM INNER")
        self._write("OC", "2025", "Q1", "clash.txt", body="ALREADY THERE")

        report = collapse_double_nestings(self.root, apply=True)

        self.assertEqual((self.root / "OC/2025/Q1/moved.txt").read_text(encoding="utf-8"), "MOVED")
        self.assertEqual(
            (self.root / "OC/2025/Q1/clash.txt").read_text(encoding="utf-8"), "ALREADY THERE",
            "an existing document was overwritten by the merge",
        )
        self.assertEqual(len(report.conflicts), 1, f"expected one conflict, got {report.conflicts}")
        # The document that could not be moved is still where it was — never lost.
        self.assertEqual(
            (self.root / "OC/OC/2025/Q1/clash.txt").read_text(encoding="utf-8"), "FROM INNER"
        )

    def test_a_source_folder_left_non_empty_by_a_conflict_is_kept(self) -> None:
        """`rmdir` on a non-empty directory raises and is swallowed — which is
        correct, but only because the document inside must not be deleted."""
        self._write("OC", "OC", "2025", "clash.txt", body="FROM INNER")
        self._write("OC", "2025", "clash.txt", body="ALREADY THERE")

        collapse_double_nestings(self.root, apply=True)

        self.assertTrue(
            (self.root / "OC/OC/2025/clash.txt").is_file(),
            "the unmergeable document was deleted with its folder",
        )


if __name__ == "__main__":
    unittest.main()
