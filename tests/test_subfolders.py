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
        self.assertEqual(normalize_category_path("Banque", 10), ("Banque",))

    def test_subfolder_under_base(self) -> None:
        self.assertEqual(normalize_category_path("Administratif/Impots", 10), ("Administratif", "Impots"))

    def test_subfolder_name_is_normalized(self) -> None:
        # accents/spaces collapse so Impôts / impots / "Impôts 2026" don't fork.
        self.assertEqual(normalize_category_path("Administratif/Impôts 2026", 10), ("Administratif", "Impots-2026"))

    def test_multi_segment_base(self) -> None:
        self.assertEqual(
            normalize_category_path("Personnel/Documents/Contrats", 10),
            ("Personnel", "Documents", "Contrats"),
        )

    def test_unknown_base_is_rejected(self) -> None:
        self.assertIsNone(normalize_category_path("Loisirs/Audio", 10))

    def test_empty_is_rejected(self) -> None:
        self.assertIsNone(normalize_category_path("   ", 10))

    def test_depth_cap_truncates(self) -> None:
        self.assertEqual(normalize_category_path("Administratif/a/b/c", 2), ("Administratif", "a"))

    def test_zero_means_uncapped(self) -> None:
        self.assertEqual(
            normalize_category_path("Administratif/a/b/c", 0),
            ("Administratif", "a", "b", "c"),
        )


class TestExistingCategoryPaths(unittest.TestCase):
    def test_lists_bases_and_subfolders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp)
            ensure_base_library_directories(lib)
            (lib / "Administratif" / "Impots").mkdir(parents=True)
            paths = existing_category_paths(lib)
            self.assertIn("Banque", paths)
            self.assertIn("Administratif", paths)
            self.assertIn("Administratif/Impots", paths)
            self.assertNotIn("Revue_Manuelle", paths)


class TestSubfolderPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_CLASSIFICATION_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_CLASSIFICATION_PRIMARY", None)
        self.tmp.cleanup()

    def _run(self, ai_path: str) -> None:
        with patch(
            "procrafiler.ai_classification.call_mistral_chat",
            return_value=json.dumps({"path": ai_path}),
        ):
            status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")

    def _files_under(self, *parts: str) -> list[Path]:
        return [p for p in (self.paths.library_root.joinpath(*parts)).rglob("*") if p.is_file()]

    def test_creates_subfolder(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"avis d'imposition 2026")
        self._run("Administratif/Impots")
        self.assertEqual(len(self._files_under("Administratif", "Impots")), 1)

    def test_flat_base_when_ai_returns_base_only(self) -> None:
        (self.paths.inbox_dir / "b.txt").write_bytes(b"document administratif")
        self._run("Administratif")
        # Filed directly under the base, no subfolder.
        self.assertEqual(len(self._files_under("Administratif")), 1)
        self.assertFalse((self.paths.library_root / "Administratif" / "Impots").exists())

    def test_unknown_base_goes_to_manual_review(self) -> None:
        (self.paths.inbox_dir / "c.txt").write_bytes(b"un truc inclassable")
        self._run("Loisirs/Audio")  # not an existing base -> rejected
        self.assertEqual(len(self._files_under("Revue_Manuelle")), 1)
        self.assertFalse((self.paths.library_root / "Loisirs").exists())

    def test_reuses_existing_subfolder(self) -> None:
        (self.paths.library_root / "Administratif" / "Impots").mkdir(parents=True)
        (self.paths.inbox_dir / "d.txt").write_bytes(b"avis d'imposition")
        self._run("Administratif/Impots")
        # Lands in the SAME folder (no near-duplicate created).
        subdirs = [p for p in (self.paths.library_root / "Administratif").iterdir() if p.is_dir()]
        self.assertEqual([p.name for p in subdirs], ["Impots"])
        self.assertEqual(len(self._files_under("Administratif", "Impots")), 1)


if __name__ == "__main__":
    unittest.main()
