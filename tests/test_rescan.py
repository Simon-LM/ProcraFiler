from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _file_sha256, run_rescan
from procrafiler.rescan import DELETED_STATUS, reconcile, walk_library_files


def _row(path: str, sha: str, status: str = "LIBRARY_STORED", doc_id: str | None = None) -> dict:
    return {
        "doc_id": doc_id or f"doc-{path}", "sha256": sha, "current_filename": Path(path).name,
        "current_path": path, "status": status, "content_json": None, "flow_state": status,
    }


class TestReconcilePure(unittest.TestCase):
    def test_known_path_is_untouched_and_never_hashed(self) -> None:
        hashed: list[Path] = []

        def sha(p: Path) -> str:
            hashed.append(p)
            return "x"

        rows = [_row("/lib/a.txt", "h1")]
        plan = reconcile([Path("/lib/a.txt")], rows, sha)
        self.assertTrue(plan.is_empty)
        self.assertEqual(hashed, [])  # a still file is never hashed

    def test_moved_file_is_repointed(self) -> None:
        rows = [_row("/lib/Old/a.txt", "h1")]
        plan = reconcile([Path("/lib/New/a.txt")], rows, lambda _p: "h1")
        self.assertEqual(len(plan.moved), 1)
        row, new_path = plan.moved[0]
        self.assertEqual(new_path, Path("/lib/New/a.txt"))
        self.assertEqual(row["doc_id"], rows[0]["doc_id"])
        self.assertEqual(plan.deleted, [])

    def test_whole_folder_rename_moves_every_file(self) -> None:
        rows = [_row("/lib/CV_LM/a.txt", "h1"), _row("/lib/CV_LM/b.txt", "h2")]
        digests = {"/lib/CV/a.txt": "h1", "/lib/CV/b.txt": "h2"}
        plan = reconcile([Path("/lib/CV/a.txt"), Path("/lib/CV/b.txt")], rows, lambda p: digests[str(p)])
        self.assertEqual(len(plan.moved), 2)
        self.assertEqual(plan.deleted, [])

    def test_new_file_is_deferred(self) -> None:
        plan = reconcile([Path("/lib/n.txt")], [], lambda _p: "hNEW")
        self.assertEqual(plan.new_files, [Path("/lib/n.txt")])

    def test_duplicate_when_original_still_present(self) -> None:
        rows = [_row("/lib/a.txt", "h1")]
        plan = reconcile([Path("/lib/a.txt"), Path("/lib/copy.txt")], rows, lambda _p: "h1")
        self.assertEqual(len(plan.duplicates), 1)
        copy_path, original = plan.duplicates[0]
        self.assertEqual(copy_path, Path("/lib/copy.txt"))
        self.assertEqual(original["current_path"], "/lib/a.txt")
        self.assertEqual(plan.moved, [])

    def test_deleted_when_gone_and_content_nowhere(self) -> None:
        rows = [_row("/lib/a.txt", "h1")]
        plan = reconcile([], rows, lambda _p: "unused")
        self.assertEqual(len(plan.deleted), 1)
        self.assertEqual(plan.moved, [])

    def test_readd_revives_a_deleted_row(self) -> None:
        rows = [_row("/lib/a.txt", "h1", status=DELETED_STATUS)]
        plan = reconcile([Path("/lib/back.txt")], rows, lambda _p: "h1")
        self.assertEqual(len(plan.readded), 1)
        self.assertEqual(plan.deleted, [])
        self.assertEqual(plan.new_files, [])


class TestRunRescanIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 6, 17, 9, 0, 0, tzinfo=timezone.utc)
        self.repo = CatalogRepository(self.paths.catalog_db_file)
        self.repo.init_schema()

    def tearDown(self) -> None:
        for key in ("PROCRAFILER_WORKSPACE_DIR", "PROCRAFILER_LIBRARY_DIR",
                    "PROCRAFILER_LIBRARY_MIRROR_DIR", "PROCRAFILER_HOME", "PROCRAFILER_CONFIG_HOME"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _emit(self, _m: str) -> None:
        pass

    def test_moved_file_repoints_catalog(self) -> None:
        lib = self.paths.library_root
        new = lib / "Personal" / "Administrative" / "Banking" / "Releve.txt"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(b"releve content")
        old = lib / "Personal" / "Releve.txt"
        self.repo.upsert_document(
            doc_id="doc-1", sha256=_file_sha256(new), current_filename="Releve.txt",
            current_path=str(old), status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
        )
        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["moved"], 1)
        self.assertEqual(self.repo.find_by_current_path(str(new))["doc_id"], "doc-1")
        self.assertIsNone(self.repo.find_by_current_path(str(old)))

    def test_deleted_file_marks_row_and_logs(self) -> None:
        old = self.paths.library_root / "Personal" / "Gone.txt"
        self.repo.upsert_document(
            doc_id="doc-2", sha256="deadbeef", current_filename="Gone.txt",
            current_path=str(old), status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
        )
        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["deleted"], 1)
        self.assertEqual(self.repo.find_by_current_path(str(old))["status"], DELETED_STATUS)
        log = self.paths.actions_log_file.read_text(encoding="utf-8")
        self.assertIn("library_file_deleted", log)
        actions = [json.loads(line)["action"] for line in log.splitlines() if line.strip()]
        self.assertIn("library_file_deleted", actions)

    def test_clean_library_is_a_noop(self) -> None:
        keep = self.paths.library_root / "Personal" / "Keep.txt"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(b"keep")
        self.repo.upsert_document(
            doc_id="doc-3", sha256=_file_sha256(keep), current_filename="Keep.txt",
            current_path=str(keep), status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
        )
        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts, {"moved": 0, "readded": 0, "duplicates": 0, "deleted": 0, "new": 0})


if __name__ == "__main__":
    unittest.main()
