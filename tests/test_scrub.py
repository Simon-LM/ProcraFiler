from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout, set_feature_flag
from procrafiler.scrub import (
    format_report,
    integrity_reminder,
    integrity_status,
    scrub,
)

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


class TestHeal(_Env):
    def _lib(self, rel: str) -> Path:
        return self.paths.library_root / rel

    def _mir(self, rel: str) -> Path:
        return self.paths.mirror_root / rel

    def test_repair_mirror_from_library(self) -> None:
        self._add("Personal/a.txt", b"good")
        self._mir("Personal/a.txt").write_bytes(b"rotten")
        report = scrub(self.paths, self.catalog, repair=True, now_utc=_NOW)
        self.assertTrue(report.healthy)
        self.assertEqual(len(report.repaired), 1)
        self.assertEqual((report.repaired[0].where, report.repaired[0].source), ("mirror", "library"))
        self.assertEqual(self._mir("Personal/a.txt").read_bytes(), b"good")  # restored

    def test_repair_library_from_mirror_and_marks_verified(self) -> None:
        doc = self._add("Personal/a.txt", b"good")
        self._lib("Personal/a.txt").write_bytes(b"corrupted")  # canonical copy goes bad
        report = scrub(self.paths, self.catalog, repair=True, now_utc=_NOW)
        self.assertTrue(report.healthy)
        self.assertEqual((report.repaired[0].where, report.repaired[0].source), ("library", "mirror"))
        self.assertEqual(self._lib("Personal/a.txt").read_bytes(), b"good")  # restored from mirror
        self.assertEqual(self._last_verified()[doc], _NOW)  # healed → counted as verified

    def test_repair_recreates_missing_mirror(self) -> None:
        self._add("Personal/a.txt", b"good")
        self._mir("Personal/a.txt").unlink()
        report = scrub(self.paths, self.catalog, repair=True, now_utc=_NOW)
        self.assertTrue(report.healthy)
        self.assertTrue(self._mir("Personal/a.txt").is_file())

    def test_both_copies_bad_is_unrecoverable(self) -> None:
        self._add("Personal/a.txt", b"good")
        self._lib("Personal/a.txt").write_bytes(b"bad1")
        self._mir("Personal/a.txt").write_bytes(b"bad2")
        report = scrub(self.paths, self.catalog, repair=True, now_utc=_NOW)
        self.assertFalse(report.healthy)
        self.assertEqual(report.repaired, [])  # no good source → nothing restored
        self.assertTrue(report.repair_attempted)
        self.assertIn("UNRECOVERABLE", format_report(report))

    def test_repair_is_logged_to_the_action_log(self) -> None:
        self._add("Personal/a.txt", b"good")
        self._mir("Personal/a.txt").write_bytes(b"rotten")
        scrub(self.paths, self.catalog, repair=True, now_utc=_NOW)
        self.assertIn("heal_restore", self.paths.actions_log_file.read_text(encoding="utf-8"))

    def test_no_repair_without_the_flag(self) -> None:
        self._add("Personal/a.txt", b"good")
        self._mir("Personal/a.txt").write_bytes(b"rotten")
        report = scrub(self.paths, self.catalog, now_utc=_NOW)  # repair defaults False
        self.assertFalse(report.healthy)
        self.assertEqual(report.repaired, [])
        self.assertEqual(self._mir("Personal/a.txt").read_bytes(), b"rotten")  # untouched


if __name__ == "__main__":
    unittest.main()


class TestIntegrityReminder(_Env):
    """Nothing ever asked for the integrity check. `scrub` existed and healed, but a
    protection nobody triggers is not a protection."""

    def _add_at(self, rel: str, content: bytes, *, filed: str) -> str:
        doc_id = self._add(rel, content)
        self.catalog.upsert_document(
            doc_id=doc_id,
            sha256=hashlib.sha256(content).hexdigest(),
            current_filename=(self.paths.library_root / rel).name,
            current_path=str(self.paths.library_root / rel),
            status="LIBRARY_STORED",
            updated_at_utc=filed,
        )
        return doc_id

    def test_a_document_never_scrubbed_ages_from_the_day_it_was_filed(self) -> None:
        """The rule that makes every document count. Filing computed the sha256 FROM
        the file, so that moment did confirm it — a document filed yesterday is not
        overdue, and one filed two years ago and never re-checked is. A separate
        "never verified" bucket would put exactly those outside the rule."""
        self._add_at("Personal/fresh.txt", b"fresh", filed="2026-06-20T00:00:00Z")

        status = integrity_status(self.catalog, now_utc=_NOW)

        self.assertEqual((status.documents, status.overdue), (1, 0))
        self.assertEqual(status.oldest_days, 4)
        self.assertIsNone(integrity_reminder(status))

    def test_a_document_filed_long_ago_and_never_checked_is_overdue(self) -> None:
        self._add_at("Personal/old.txt", b"old", filed="2026-01-01T00:00:00Z")

        status = integrity_status(self.catalog, now_utc=_NOW)

        self.assertEqual((status.documents, status.overdue), (1, 1))
        self.assertIn("1 of 1 document(s) unverified", str(integrity_reminder(status)))
        self.assertIn("procrafiler scrub", str(integrity_reminder(status)))

    def test_a_scrub_clears_the_reminder(self) -> None:
        """Anti-vacuity: the nudge must answer to the command it names."""
        self._add_at("Personal/old.txt", b"old", filed="2026-01-01T00:00:00Z")
        self.assertTrue(integrity_status(self.catalog, now_utc=_NOW).is_overdue)

        scrub(self.paths, self.catalog, now_utc=_NOW)

        self.assertFalse(integrity_status(self.catalog, now_utc=_NOW).is_overdue)

    def test_the_two_timestamp_shapes_in_the_catalog_are_both_read(self) -> None:
        """`updated_at_utc` is written as `…Z` and `last_verified_utc` as `…+00:00`.
        Comparing them as TEXT in SQL would rank the same instant differently
        depending on which column it came from."""
        self._add_at("Personal/z.txt", b"z", filed="2026-06-20T00:00:00Z")
        offset_doc = self._add_at("Personal/off.txt", b"off", filed="2026-01-01T00:00:00Z")
        self.catalog.mark_verified([offset_doc], when_utc="2026-06-20T00:00:00+00:00")

        status = integrity_status(self.catalog, now_utc=_NOW)

        self.assertEqual(status.overdue, 0, "one of the two shapes was misread")
        self.assertEqual(status.oldest_days, 4)

    def test_exactly_at_the_threshold_counts_as_overdue(self) -> None:
        self._add_at("Personal/edge.txt", b"edge", filed="2026-05-25T12:00:00Z")  # 30 days
        self.assertEqual(integrity_status(self.catalog, now_utc=_NOW).overdue, 1)

    def test_one_day_short_of_the_threshold_does_not(self) -> None:
        self._add_at("Personal/edge.txt", b"edge", filed="2026-05-26T12:00:00Z")  # 29 days
        self.assertEqual(integrity_status(self.catalog, now_utc=_NOW).overdue, 0)

    def test_an_unreadable_timestamp_counts_as_unconfirmed(self) -> None:
        """Never under-report: a document whose age cannot be established has not
        been confirmed either."""
        self._add_at("Personal/bad.txt", b"bad", filed="not-a-date")
        self.assertEqual(integrity_status(self.catalog, now_utc=_NOW).overdue, 1)

    def test_an_empty_catalog_reports_nothing_to_check(self) -> None:
        status = integrity_status(self.catalog, now_utc=_NOW)
        self.assertEqual((status.documents, status.overdue), (0, 0))
        self.assertIsNone(integrity_reminder(status))
