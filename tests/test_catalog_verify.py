from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.catalog_verify import format_report, verify_catalog
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _write_catalog_snapshot

_NOW = "2026-06-24T12:00:00+00:00"


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        self._snapshot = {k: v for k, v in os.environ.items() if k.startswith("PROCRAFILER_")}
        for k in list(os.environ):
            if k.startswith("PROCRAFILER_"):
                del os.environ[k]
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        for var, sub in (("WORKSPACE_DIR", "Inbox"), ("LIBRARY_DIR", "Library"),
                         ("LIBRARY_MIRROR_DIR", "Mirror"), ("HOME", "state"), ("CONFIG_HOME", "config")):
            os.environ[f"PROCRAFILER_{var}"] = str(tmp / sub)
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.catalog = CatalogRepository(self.paths.catalog_db_file)
        self.catalog.init_schema()

    def tearDown(self) -> None:
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_")]:
            del os.environ[k]
        os.environ.update(self._snapshot)
        self._tmp.cleanup()

    def _add(self, doc_id: str, content_json: str | None = None) -> None:
        self.catalog.upsert_document(
            doc_id=doc_id, sha256="hash-" + doc_id, current_filename=f"{doc_id}.txt",
            current_path=f"/lib/{doc_id}.txt", status="LIBRARY_STORED",
            updated_at_utc="2026-01-01T00:00:00+00:00", content_json=content_json,
        )

    def _write_snapshot(self) -> None:
        _write_catalog_snapshot(self.paths, self.catalog)

    def _corrupt_db(self) -> None:
        self.paths.catalog_db_file.write_bytes(b"this is not a sqlite database")


class TestVerifyCatalog(_Env):
    def test_healthy_catalog_is_ok(self) -> None:
        self._add("a")
        self._add("b")
        self._write_snapshot()
        report = verify_catalog(self.paths)
        self.assertTrue(report.ok)
        self.assertTrue(report.healthy)
        self.assertEqual((report.db_count, report.snapshot_count), (2, 2))
        self.assertIn("integrity OK", format_report(report))

    def test_fresh_empty_catalog_is_healthy(self) -> None:
        report = verify_catalog(self.paths)  # 0 docs, empty snapshot file
        self.assertTrue(report.healthy)
        self.assertFalse(report.rebuilt)

    def test_corrupt_db_is_recoverable_then_rebuilt_with_fiche(self) -> None:
        self._add("a", content_json='{"name": "Doc A", "keywords": ["x"]}')
        self._write_snapshot()
        self._corrupt_db()

        detect = verify_catalog(self.paths)  # no --rebuild
        self.assertFalse(detect.ok)
        self.assertTrue(detect.recoverable)
        self.assertFalse(detect.rebuilt)
        self.assertIn("--rebuild", format_report(detect))

        rebuilt = verify_catalog(self.paths, rebuild=True, now_utc=_NOW)
        self.assertTrue(rebuilt.rebuilt)
        self.assertTrue(rebuilt.ok)
        self.assertEqual(rebuilt.rebuilt_count, 1)
        self.assertTrue(Path(str(rebuilt.backup_path)).exists())  # old (corrupt) DB kept

        fresh = CatalogRepository(self.paths.catalog_db_file)
        self.assertTrue(fresh.integrity_ok())
        self.assertEqual(fresh.count_documents(), 1)
        restored = fresh.list_documents()[0]
        self.assertEqual(json.loads(str(restored["content_json"])), {"name": "Doc A", "keywords": ["x"]})

    def test_corrupt_db_without_snapshot_is_unrecoverable(self) -> None:
        self._add("a")  # no snapshot written
        self._corrupt_db()
        report = verify_catalog(self.paths, rebuild=True)
        self.assertFalse(report.ok)
        self.assertFalse(report.recoverable)
        self.assertFalse(report.rebuilt)
        self.assertIn("UNRECOVERABLE", format_report(report))

    def test_missing_db_with_snapshot_rebuilds(self) -> None:
        self._add("a")
        self._add("b")
        self._write_snapshot()
        self.paths.catalog_db_file.unlink()
        report = verify_catalog(self.paths, rebuild=True, now_utc=_NOW)
        self.assertTrue(report.rebuilt)
        self.assertTrue(report.ok)
        self.assertEqual(CatalogRepository(self.paths.catalog_db_file).count_documents(), 2)


if __name__ == "__main__":
    unittest.main()
