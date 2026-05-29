# pyright: reportUnknownVariableType=false
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.cli import main
from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    set_feature_flag,
)
from procrafiler.pipeline import (
    process_next_inbox_file,
    reconcile_catalog_snapshot,
)


class TestSnapshotReconcile(unittest.TestCase):
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
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_FAKE_NOW", None)
        self.tmp.cleanup()

    def _seed_one_document(self) -> str:
        (self.paths.inbox_dir / "doc.pdf").write_bytes(b"content")
        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")
        return status

    def test_reports_missing_when_snapshot_empty(self) -> None:
        # ensure_runtime_layout touches the snapshot file (size 0). Treat as missing.
        result = reconcile_catalog_snapshot(self.paths, now_utc=self.now)
        self.assertEqual(result.reason, "missing")
        self.assertTrue(result.rewrote_snapshot)
        self.assertIsNone(result.documents_in_snapshot_before)

    def test_reports_consistent_after_a_fresh_pipeline_run(self) -> None:
        self._seed_one_document()
        # The pipeline already wrote a fresh snapshot. Reconcile must be no-op.
        result = reconcile_catalog_snapshot(self.paths, now_utc=self.now)
        self.assertEqual(result.reason, "consistent")
        self.assertFalse(result.rewrote_snapshot)
        self.assertEqual(result.documents_in_db, 1)
        self.assertEqual(result.documents_in_snapshot_before, 1)

    def test_rewrites_when_doc_set_differs(self) -> None:
        self._seed_one_document()
        # Manually corrupt snapshot so its document set doesn't match the DB.
        self.paths.catalog_snapshot_file.write_text(
            json.dumps({"meta": {"documents_count": 0}, "documents": []}),
            encoding="utf-8",
        )

        result = reconcile_catalog_snapshot(self.paths, now_utc=self.now)
        self.assertEqual(result.reason, "content_mismatch")
        self.assertTrue(result.rewrote_snapshot)

        # Snapshot now has the missing doc back.
        repaired = json.loads(self.paths.catalog_snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(len(repaired["documents"]), 1)

    def test_rewrites_when_updated_at_drifts(self) -> None:
        self._seed_one_document()
        snapshot = json.loads(self.paths.catalog_snapshot_file.read_text(encoding="utf-8"))
        snapshot["documents"][0]["updated_at_utc"] = "1999-01-01T00:00:00Z"
        self.paths.catalog_snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

        result = reconcile_catalog_snapshot(self.paths, now_utc=self.now)
        self.assertEqual(result.reason, "content_mismatch")
        self.assertTrue(result.rewrote_snapshot)

    def test_rewrites_when_snapshot_unreadable(self) -> None:
        self._seed_one_document()
        self.paths.catalog_snapshot_file.write_text("{ not json", encoding="utf-8")

        result = reconcile_catalog_snapshot(self.paths, now_utc=self.now)
        self.assertEqual(result.reason, "unreadable")
        self.assertTrue(result.rewrote_snapshot)
        self.assertIsNone(result.documents_in_snapshot_before)

        # The rewritten file is valid JSON again.
        repaired = json.loads(self.paths.catalog_snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(len(repaired["documents"]), 1)

    def test_skipped_when_feature_disabled(self) -> None:
        set_feature_flag(self.paths, "catalog_snapshot", False)
        # Even with an unreadable snapshot, we don't touch it when the feature is off.
        self.paths.catalog_snapshot_file.write_text("garbage", encoding="utf-8")

        result = reconcile_catalog_snapshot(self.paths, now_utc=self.now)
        self.assertEqual(result.reason, "feature_disabled")
        self.assertFalse(result.rewrote_snapshot)
        # Snapshot file untouched.
        self.assertEqual(self.paths.catalog_snapshot_file.read_text(encoding="utf-8"), "garbage")

    def test_logs_action_when_rewriting(self) -> None:
        self._seed_one_document()
        self.paths.catalog_snapshot_file.write_text("{ broken", encoding="utf-8")
        # Capture log entries created from this point on.
        existing_lines = self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()

        reconcile_catalog_snapshot(self.paths, now_utc=self.now)

        new_lines = self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()[len(existing_lines):]
        reconciled_events = [
            json.loads(line) for line in new_lines if line.strip() and json.loads(line).get("action") == "snapshot_reconciled"
        ]
        self.assertEqual(len(reconciled_events), 1)
        self.assertEqual(reconciled_events[0]["reason"], "unreadable")

    def test_cli_reconcile_snapshot_consistent(self) -> None:
        self._seed_one_document()
        os.environ["PROCRAFILER_FAKE_NOW"] = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["reconcile-snapshot"])
        self.assertEqual(code, 0)
        self.assertIn("already consistent", stdout.getvalue())

    def test_cli_reconcile_snapshot_rewrites(self) -> None:
        self._seed_one_document()
        self.paths.catalog_snapshot_file.write_text("{ corrupt", encoding="utf-8")
        os.environ["PROCRAFILER_FAKE_NOW"] = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["reconcile-snapshot"])
        self.assertEqual(code, 0)
        self.assertIn("regenerated", stdout.getvalue())
        self.assertIn("unreadable", stdout.getvalue())

    def test_process_once_auto_reconciles_stale_snapshot(self) -> None:
        # Seed one doc, then corrupt the snapshot so the NEXT process-once
        # invocation has to fix it before doing its work.
        self._seed_one_document()
        self.paths.catalog_snapshot_file.write_text("{ corrupt", encoding="utf-8")

        # Drop a new file in the inbox so process-once has actual work.
        (self.paths.inbox_dir / "second.pdf").write_bytes(b"another")
        os.environ["PROCRAFILER_FAKE_NOW"] = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["process-once"])
        self.assertEqual(code, 0)

        # Snapshot is now valid JSON again AND reflects two documents.
        snapshot = json.loads(self.paths.catalog_snapshot_file.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["meta"]["documents_count"], 2)


if __name__ == "__main__":
    unittest.main()
