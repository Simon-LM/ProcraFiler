# pyright: reportUnknownVariableType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_next_inbox_file


def _make_jpeg_with_exif(path: Path, dt_str: str) -> None:
    image = Image.new("RGB", (4, 4), color="red")
    exif = image.getexif()
    exif[0x8769] = {36867: dt_str}  # Exif sub-IFD: DateTimeOriginal
    image.save(path, exif=exif)


class TestExifAndOrigin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        # No AI chain configured: files go to Manual_Review, but EXIF dating and
        # the origin-folder capture happen regardless of the analysis.
        for key in [k for k in os.environ if k.startswith("PROCRAFILER_AI_")]:
            os.environ.pop(key, None)
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _only_fiche(self) -> dict:
        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        docs = repo.list_documents()
        self.assertEqual(len(docs), 1)
        return json.loads(docs[0]["content_json"])

    def test_photo_filename_uses_exif_capture_date(self) -> None:
        sub = self.paths.inbox_dir / "Event"
        sub.mkdir()
        _make_jpeg_with_exif(sub / "photo.jpg", "2025:08:01 20:39:18")
        # An mtime far from the capture date — EXIF must still win for the prefix.
        mtime = datetime(2026, 2, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        os.utime(sub / "photo.jpg", (mtime, mtime))

        process_next_inbox_file(self.paths, now_utc=self.now)

        filed = [p for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertEqual(len(filed), 1)
        self.assertTrue(filed[0].name.startswith("2025-08-01_20-39-18__"), filed[0].name)
        fiche = self._only_fiche()
        self.assertEqual(fiche["effective_date"], "2025-08-01")

    def test_source_folder_recorded_for_subfolder(self) -> None:
        sub = self.paths.inbox_dir / "Water-Damage"
        sub.mkdir()
        (sub / "note.txt").write_bytes(b"constat")
        process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(self._only_fiche()["source_folder"], "Water-Damage")

    def test_source_folder_is_relative_for_nested_subfolder(self) -> None:
        sub = self.paths.inbox_dir / "Claim" / "provisoir"
        sub.mkdir(parents=True)
        (sub / "note.txt").write_bytes(b"photo provisoire")
        process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(self._only_fiche()["source_folder"], str(Path("Claim") / "provisoir"))

    def test_root_file_has_no_source_folder(self) -> None:
        (self.paths.inbox_dir / "loose.txt").write_bytes(b"contenu")
        process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertIsNone(self._only_fiche()["source_folder"])


if __name__ == "__main__":
    unittest.main()
