# pyright: reportUnknownVariableType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_next_inbox_file
from procrafiler.taxonomy import (
    ensure_base_library_directories,
    existing_category_paths,
    normalize_category_path,
)


class TestNormalizeCategoryPath(unittest.TestCase):
    def test_flat_base(self) -> None:
        self.assertEqual(normalize_category_path("Work", 10), ("Work",))

    def test_subfolder_under_base(self) -> None:
        # base "Personal/Hobbies" + a new subfolder "Photography".
        self.assertEqual(
            normalize_category_path("Personal/Hobbies/Photography", 10),
            ("Personal", "Hobbies", "Photography"),
        )

    def test_subfolder_name_is_normalized(self) -> None:
        # accents/spaces collapse so "Jeux Vidéo" / "jeux video" don't fork.
        self.assertEqual(
            normalize_category_path("Personal/Hobbies/Jeux Vidéo", 10),
            ("Personal", "Hobbies", "Jeux-Video"),
        )

    def test_longest_base_prefix_wins(self) -> None:
        # "Personal" and "Personal/Administrative/Taxes" are both bases; the
        # longest matching prefix is chosen, the rest become subfolders.
        self.assertEqual(
            normalize_category_path("Personal/Administrative/Taxes/2026", 10),
            ("Personal", "Administrative", "Taxes", "2026"),
        )

    def test_unknown_base_is_rejected(self) -> None:
        self.assertIsNone(normalize_category_path("Music/Jazz", 10))

    def test_empty_is_rejected(self) -> None:
        self.assertIsNone(normalize_category_path("   ", 10))

    def test_depth_cap_truncates(self) -> None:
        self.assertEqual(
            normalize_category_path("Work/Business/Clients/a/b", 4),
            ("Work", "Business", "Clients", "a"),
        )

    def test_zero_means_uncapped(self) -> None:
        self.assertEqual(
            normalize_category_path("Work/Business/Clients/a/b", 0),
            ("Work", "Business", "Clients", "a", "b"),
        )


class TestExistingCategoryPaths(unittest.TestCase):
    def test_lists_bases_and_subfolders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            ensure_base_library_directories(lib)
            (lib / "Personal" / "Hobbies" / "Photography").mkdir(parents=True)
            paths = existing_category_paths(lib)
            self.assertIn("Work", paths)
            self.assertIn("Personal/Administrative", paths)
            self.assertIn("Personal/Administrative/Taxes", paths)
            self.assertIn("Personal/Hobbies/Photography", paths)
            self.assertNotIn("Manual_Review", paths)
            # De-duplicated even though the tree is nested (Personal/Administrative
            # is reachable both as a base and as a child of Personal).
            self.assertEqual(len(paths), len(set(paths)))


class TestSubfolderPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        self.tmp.cleanup()

    def _run(self, ai_path: str) -> None:
        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value=json.dumps({"name": "Doc", "category_path": ai_path}),
        ):
            status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")

    def _files_under(self, *parts: str) -> list[Path]:
        return [p for p in (self.paths.library_root.joinpath(*parts)).rglob("*") if p.is_file()]

    def test_creates_subfolder(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"avis d'imposition 2026")
        self._run("Personal/Administrative/Taxes/2026")
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Taxes", "2026")), 1)

    def test_flat_base_when_ai_returns_base_only(self) -> None:
        (self.paths.inbox_dir / "b.txt").write_bytes(b"document de travail")
        self._run("Work")
        # Filed directly under the base, not in a subfolder.
        files = self._files_under("Work")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, self.paths.library_root / "Work")

    def test_unknown_base_goes_to_manual_review(self) -> None:
        (self.paths.inbox_dir / "c.txt").write_bytes(b"un truc inclassable")
        self._run("Music/Jazz")  # not an existing base -> rejected
        self.assertEqual(len(self._files_under("Manual_Review")), 1)
        self.assertFalse((self.paths.library_root / "Music").exists())

    def test_reuses_existing_subfolder(self) -> None:
        (self.paths.library_root / "Personal" / "Administrative" / "Taxes" / "2026").mkdir(parents=True)
        (self.paths.inbox_dir / "d.txt").write_bytes(b"avis d'imposition")
        self._run("Personal/Administrative/Taxes/2026")
        # Lands in the SAME folder (no near-duplicate created).
        taxes = self.paths.library_root / "Personal" / "Administrative" / "Taxes"
        subdirs = [p.name for p in taxes.iterdir() if p.is_dir()]
        self.assertEqual(subdirs, ["2026"])
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Taxes", "2026")), 1)


if __name__ == "__main__":
    unittest.main()
