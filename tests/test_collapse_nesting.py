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


if __name__ == "__main__":
    unittest.main()
