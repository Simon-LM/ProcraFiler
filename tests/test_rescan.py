from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _file_sha256, run_rescan
from procrafiler.rescan import DELETED_STATUS, reconcile, walk_library_files


def _row(path: str, sha: str, status: str = "LIBRARY_STORED", doc_id: str | None = None) -> dict:
    return {
        "doc_id": doc_id or f"doc-{path}", "sha256": sha, "current_filename": Path(path).name,
        "current_path": path, "status": status, "content_json": None, "flow_state": status,
    }


class TestWalkLibraryFiles(unittest.TestCase):
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

    def test_skips_hidden_files_and_dirs(self) -> None:
        doc = self._touch("Personal", "note.txt")
        self._touch("Personal", ".hidden.txt")
        self._touch("Personal", ".config", "settings.ini")
        self.assertEqual(walk_library_files(self.root), [doc])

    def test_never_descends_into_a_git_repo(self) -> None:
        # The run-15 disaster: a dropped folder containing a .git had its repo
        # internals AND working tree timestamped. The whole repo is left alone.
        doc = self._touch("Personal", "keep.txt")
        self._touch("Work", "Backup", "repo", ".git", "HEAD")
        self._touch("Work", "Backup", "repo", ".git", "objects", "ab", "deadbeef")
        self._touch("Work", "Backup", "repo", "src", "main.py")  # working tree
        self._touch("Work", "Backup", "repo", "README.md")
        self.assertEqual(walk_library_files(self.root), [doc])


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

    def test_rename_in_place_plus_copy_elsewhere_is_move_plus_duplicate(self) -> None:
        # Edge case (real run): the catalogued path is gone (renamed in place) AND
        # a copy of the same content appears elsewhere. One copy is taken as the
        # move; the OTHER is a duplicate — never a brand-new file to re-ingest and
        # re-timestamp (which produced a doubled prefix).
        rows = [_row("/lib/CAF/2025__Facture_CAF.pdf", "hX")]
        disk = [Path("/lib/Admin/AR.pdf"), Path("/lib/CAF/2025__AR_CAF.pdf")]
        plan = reconcile(disk, rows, lambda _p: "hX")
        self.assertEqual(len(plan.moved), 1)
        self.assertEqual(len(plan.duplicates), 1)
        self.assertEqual(plan.new_files, [])  # nothing re-ingested
        _copy, original = plan.duplicates[0]
        self.assertEqual(original["doc_id"], rows[0]["doc_id"])


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
                    "PROCRAFILER_LIBRARY_MIRROR_DIR", "PROCRAFILER_HOME", "PROCRAFILER_CONFIG_HOME",
                    "PROCRAFILER_AI_ANALYSIS_PRIMARY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _emit(self, _m: str) -> None:
        pass

    def test_moved_file_repoints_catalog(self) -> None:
        # An already-prefixed file just dragged to a new folder: pure repoint, no
        # rename (its prefix is valid and is never re-dated).
        lib = self.paths.library_root
        new = lib / "Personal" / "Administrative" / "Banking" / "2026-04-30_00-00-00__Releve.txt"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(b"releve content")
        old = lib / "Personal" / "2026-04-30_00-00-00__Releve.txt"
        self.repo.upsert_document(
            doc_id="doc-1", sha256=_file_sha256(new), current_filename=new.name,
            current_path=str(old), status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
        )
        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["moved"], 1)
        self.assertTrue(new.is_file())  # not renamed
        self.assertEqual(self.repo.find_by_current_path(str(new))["doc_id"], "doc-1")
        self.assertIsNone(self.repo.find_by_current_path(str(old)))

    def test_renamed_file_without_prefix_gets_one_from_its_fiche(self) -> None:
        # run-17: you renamed a filed doc to `AR.pdf` (no prefix). The horodatage
        # is the app's — rescan re-applies it from the fiche date, keeping `AR`.
        import json as _json
        lib = self.paths.library_root
        new = lib / "Personal" / "Administrative" / "AR.pdf"
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(b"caf content")
        old = lib / "Personal" / "Administrative" / "Housing" / "CAF" / "2025-06-07_00-00-00__Facture_CAF.pdf"
        self.repo.upsert_document(
            doc_id="doc-caf", sha256=_file_sha256(new), current_filename="AR.pdf",
            current_path=str(old), status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
            content_json=_json.dumps({"effective_date": "2025-06-07"}),
        )
        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["moved"], 1)
        prefixed = new.parent / "2025-06-07_00-00-00__AR.pdf"
        self.assertTrue(prefixed.is_file())
        self.assertFalse(new.exists())
        self.assertEqual(self.repo.find_by_current_path(str(prefixed))["doc_id"], "doc-caf")

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

    def test_new_file_is_ingested_with_timestamp_prefix(self) -> None:
        # Phase 2: a brand-new hand-placed file (no AI chain → minimal fiche) is
        # timestamped in place, the user's stem kept, and catalogued.
        folder = self.paths.library_root / "Personal" / "Misc"
        folder.mkdir(parents=True, exist_ok=True)
        placed = folder / "Ma note.txt"
        placed.write_text("une note libre", encoding="utf-8")
        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["new"], 1)
        self.assertFalse(placed.exists())  # renamed (prefix added)
        ingested = [p for p in folder.iterdir() if p.name.endswith("__Ma-note.txt")]
        self.assertEqual(len(ingested), 1)
        row = self.repo.find_by_current_path(str(ingested[0]))
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "LIBRARY_STORED")
        self.assertIn("library_file_ingested", self.paths.actions_log_file.read_text(encoding="utf-8"))

    def test_new_series_file_descends_into_year_subfolder(self) -> None:
        # Phase 2: a new series file placed by hand in its entity folder is dated
        # into <Entity>/<Year>/, exactly like the run — the user's folder anchors it.
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        edf = self.paths.library_root / "Personal" / "Administrative" / "Utilities" / "EDF"
        edf.mkdir(parents=True, exist_ok=True)
        (edf / "facture.txt").write_text("facture edf avril", encoding="utf-8")
        fiche = json.dumps({
            "name": "Facture_EDF", "date": "2026-04-05",
            "category_path": "Personal/Administrative/Utilities", "series": True,
            "alternatives": [], "summary": "Facture EDF avril.", "keywords": ["facture", "edf"],
            "entities": {"issuer": "EDF"}, "language": "fr",
        })
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=fiche):
            counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["new"], 1)
        target = edf / "2026" / "2026-04-05_00-00-00__facture.txt"
        self.assertTrue(target.is_file())
        self.assertIsNotNone(self.repo.find_by_current_path(str(target)))

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
