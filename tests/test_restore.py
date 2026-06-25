from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.restore import (
    mirror_snapshot_path,
    replicate_catalog_to_mirror,
    restore_from_mirror,
)

_NOW = "2026-06-24T12:00:00+00:00"
_DOCS = {"Personal/a.txt": b"alpha", "Work/sub/b.txt": b"beta"}


def _set_env(base: Path) -> None:
    for var, sub in (("WORKSPACE_DIR", "Inbox"), ("LIBRARY_DIR", "Library"),
                     ("LIBRARY_MIRROR_DIR", "Mirror"), ("HOME", "state"), ("CONFIG_HOME", "config")):
        os.environ[f"PROCRAFILER_{var}"] = str(base / sub)


class TestRestore(unittest.TestCase):
    def setUp(self) -> None:
        self._snapshot = {k: v for k, v in os.environ.items() if k.startswith("PROCRAFILER_")}
        for k in list(os.environ):
            if k.startswith("PROCRAFILER_"):
                del os.environ[k]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

        # --- Build the SOURCE library + mirror, and replicate its catalog. ---
        _set_env(self.tmp / "src")
        self.src = default_runtime_paths()
        ensure_runtime_layout(self.src)
        cat = CatalogRepository(self.src.catalog_db_file)
        cat.init_schema()
        for rel, content in _DOCS.items():
            for root in (self.src.library_root, self.src.mirror_root):
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(content)
            cat.upsert_document(
                doc_id=rel, sha256=hashlib.sha256(content).hexdigest(),
                current_filename=Path(rel).name, current_path=str(self.src.library_root / rel),
                status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00+00:00",
                content_json='{"name": "Doc"}',
            )
        self.assertTrue(replicate_catalog_to_mirror(self.src))
        self.mirror_dir = self.src.mirror_root

    def tearDown(self) -> None:
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_")]:
            del os.environ[k]
        os.environ.update(self._snapshot)
        self._tmp.cleanup()

    def test_replicate_wrote_a_self_contained_catalog(self) -> None:
        snap = mirror_snapshot_path(self.mirror_dir)
        self.assertTrue(snap.is_file())
        data = json.loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(len(data["documents"]), 2)
        self.assertEqual(data["meta"]["library_root"], str(self.src.library_root))

    def test_restore_to_a_new_location_rebuilds_files_catalog_and_reroots(self) -> None:
        # Simulate losing the primary: restore the mirror into a fresh, different root.
        _set_env(self.tmp / "dst")
        dst = default_runtime_paths()
        ensure_runtime_layout(dst)
        report = restore_from_mirror(dst, self.mirror_dir, now_utc=_NOW)

        self.assertEqual(report.files_copied, 2)  # the documents, not .procrafiler
        self.assertEqual(report.documents_restored, 2)
        # files are back, with content intact, under the NEW root
        self.assertEqual((dst.library_root / "Personal/a.txt").read_bytes(), b"alpha")
        self.assertEqual((dst.library_root / "Work/sub/b.txt").read_bytes(), b"beta")
        # catalog rebuilt and re-rooted to the new library
        cat = CatalogRepository(dst.catalog_db_file)
        self.assertTrue(cat.integrity_ok())
        docs = cat.list_documents()
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(str(d["current_path"]).startswith(str(dst.library_root)) for d in docs))
        self.assertEqual(json.loads(str(docs[0]["content_json"])), {"name": "Doc"})

    def test_restore_does_not_copy_the_metadata_folder(self) -> None:
        _set_env(self.tmp / "dst")
        dst = default_runtime_paths()
        ensure_runtime_layout(dst)
        restore_from_mirror(dst, self.mirror_dir, now_utc=_NOW)
        self.assertFalse((dst.library_root / ".procrafiler").exists())

    def test_restore_without_replicated_catalog_raises(self) -> None:
        plain = self.tmp / "plain"
        (plain / "Personal").mkdir(parents=True)
        (plain / "Personal" / "a.txt").write_bytes(b"x")
        _set_env(self.tmp / "dst")
        dst = default_runtime_paths()
        ensure_runtime_layout(dst)
        with self.assertRaises(FileNotFoundError):
            restore_from_mirror(dst, plain, now_utc=_NOW)


if __name__ == "__main__":
    unittest.main()
