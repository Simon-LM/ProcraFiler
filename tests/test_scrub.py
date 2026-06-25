from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout, set_feature_flag
from procrafiler.scrub import format_report, scrub

_NOW = "2026-06-24T12:00:00+00:00"


class _Env(unittest.TestCase):
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
        self.catalog = CatalogRepository(self.paths.catalog_db_file)
        self.catalog.init_schema()

    def tearDown(self) -> None:
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_")]:
            del os.environ[k]
        os.environ.update(self._snapshot)
        self._tmp.cleanup()

    def _add(self, rel: str, content: bytes, *, mirror: bool = True) -> str:
        lib = self.paths.library_root / rel
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_bytes(content)
        if mirror:
            mir = self.paths.mirror_root / rel
            mir.parent.mkdir(parents=True, exist_ok=True)
            mir.write_bytes(content)
        doc_id = str(uuid4())
        self.catalog.upsert_document(
            doc_id=doc_id,
            sha256=hashlib.sha256(content).hexdigest(),
            current_filename=lib.name,
            current_path=str(lib),
            status="LIBRARY_STORED",
            updated_at_utc="2026-01-01T00:00:00+00:00",
        )
        return doc_id

    def _last_verified(self) -> dict[str, str | None]:
        return {str(d["doc_id"]): d["last_verified_utc"] for d in self.catalog.documents_for_scrub()}


class TestScrub(_Env):
    def test_clean_library_all_ok_and_marked_verified(self) -> None:
        a = self._add("Personal/a.txt", b"hello")
        b = self._add("Work/b.txt", b"world")
        report = scrub(self.paths, self.catalog, now_utc=_NOW)
        self.assertTrue(report.healthy)
        self.assertEqual((report.checked, report.library_ok, report.mirror_ok), (2, 2, 2))
        self.assertEqual(self._last_verified(), {a: _NOW, b: _NOW})

    def test_corrupt_library_file_detected_and_not_verified(self) -> None:
        doc = self._add("Personal/a.txt", b"original")
        (self.paths.library_root / "Personal/a.txt").write_bytes(b"tampered")  # hash now differs
        report = scrub(self.paths, self.catalog, now_utc=_NOW)
        self.assertFalse(report.healthy)
        self.assertEqual(report.corrupt, 1)
        issue = next(i for i in report.issues if i.where == "library")
        self.assertEqual(issue.state, "corrupt")
        self.assertIsNone(self._last_verified()[doc])  # corrupt → not marked verified

    def test_missing_library_file_detected(self) -> None:
        self._add("Personal/a.txt", b"x")
        (self.paths.library_root / "Personal/a.txt").unlink()
        report = scrub(self.paths, self.catalog, now_utc=_NOW)
        self.assertEqual(report.missing, 1)
        self.assertEqual(next(i for i in report.issues if i.where == "library").state, "missing")

    def test_corrupt_mirror_detected_library_still_ok(self) -> None:
        self._add("Personal/a.txt", b"good")
        (self.paths.mirror_root / "Personal/a.txt").write_bytes(b"rotten")
        report = scrub(self.paths, self.catalog, now_utc=_NOW)
        self.assertEqual(report.library_ok, 1)  # the canonical copy is fine
        mirror_issues = [i for i in report.issues if i.where == "mirror"]
        self.assertEqual(len(mirror_issues), 1)
        self.assertEqual(mirror_issues[0].state, "corrupt")

    def test_no_mirror_flag_skips_mirror(self) -> None:
        self._add("Personal/a.txt", b"good")
        (self.paths.mirror_root / "Personal/a.txt").write_bytes(b"rotten")
        report = scrub(self.paths, self.catalog, check_mirror=False, now_utc=_NOW)
        self.assertTrue(report.healthy)
        self.assertEqual(report.mirror_checked, 0)

    def test_mirror_disabled_setting_skips_mirror(self) -> None:
        set_feature_flag(self.paths, "mirror_sync", False)
        self._add("Personal/a.txt", b"good", mirror=False)
        report = scrub(self.paths, self.catalog, now_utc=_NOW)
        self.assertTrue(report.healthy)
        self.assertFalse(report.mirror_enabled)

    def test_limit_checks_least_recently_verified_first(self) -> None:
        ids = {self._add(f"d{i}.txt", f"c{i}".encode()) for i in range(3)}
        first = scrub(self.paths, self.catalog, limit=1, now_utc=_NOW)
        self.assertEqual(first.checked, 1)
        verified_after_first = {k for k, v in self._last_verified().items() if v is not None}
        self.assertEqual(len(verified_after_first), 1)
        # a second limited pass picks a still-unverified doc (NULL sorts first)
        scrub(self.paths, self.catalog, limit=1, now_utc="2026-06-25T00:00:00+00:00")
        verified_after_second = {k for k, v in self._last_verified().items() if v is not None}
        self.assertEqual(len(verified_after_second), 2)
        self.assertTrue(verified_after_second.issubset(ids))

    def test_format_report_healthy_and_problem(self) -> None:
        self._add("Personal/a.txt", b"x")
        self.assertIn("✓", format_report(scrub(self.paths, self.catalog, now_utc=_NOW)))
        (self.paths.library_root / "Personal/a.txt").unlink()
        self.assertIn("PROBLEMS", format_report(scrub(self.paths, self.catalog, now_utc=_NOW)))


if __name__ == "__main__":
    unittest.main()
