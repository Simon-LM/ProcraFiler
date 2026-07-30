# pyright: reportUnknownVariableType=false
"""A run must be reversible.

Until now `process-all` was irreversible in practice: putting a library back meant
moving every document by hand and then `rescan`. That is what makes a first run on
real documents frightening — not that the AI might be wrong, but that being wrong
would cost an evening of manual repair.

The two properties that matter, and that these tests pin:

- **it puts things back where they came from**, including the Inbox *subfolder*,
  so files dropped together stay a set for the next run;
- **it refuses rather than guesses**: a document changed since the run is reported
  and left strictly alone. An undo that did its best on a library the user has
  since reorganised would be worse than no undo at all.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import procrafiler.pipeline as pipeline
from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files
from procrafiler.undo import apply_undo, latest_run_id, list_runs, plan_undo


class _Workspace(unittest.TestCase):
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
        self.now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _drop(self, relative: str, body: str = "content") -> Path:
        target = self.paths.inbox_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return target

    def _library_files(self) -> list[Path]:
        return sorted(
            p for p in self.paths.library_root.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        )

    def _inbox_relatives(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.paths.inbox_dir))
            for p in self.paths.inbox_dir.rglob("*") if p.is_file()
        )

    def _run(self) -> str:
        process_all_inbox_files(self.paths, now_utc=self.now)
        run_id = latest_run_id(self.paths)
        self.assertIsNotNone(run_id, "the run did not record a run id")
        assert run_id is not None
        return run_id


class TestTheRoundTrip(_Workspace):
    def test_every_document_goes_back_to_the_subfolder_it_came_from(self) -> None:
        """The subfolder is the point: files dropped together are a SET, and
        dumping them back at the Inbox root would silently break the grouping the
        next run depends on."""
        self._drop("Degats-eaux/facture.txt", "a")
        self._drop("Degats-eaux/constat.txt", "b")
        self._drop("loose.txt", "c")
        run_id = self._run()
        self.assertEqual(len(self._library_files()), 3, "setup failed: nothing was filed")

        report = apply_undo(self.paths, plan_undo(self.paths, run_id))

        self.assertEqual(report.restored, 3)
        self.assertEqual(self._library_files(), [], "documents were left in the library")
        self.assertEqual(
            self._inbox_relatives(),
            ["Degats-eaux/constat.txt", "Degats-eaux/facture.txt", "loose.txt"],
        )

    def test_the_catalog_forgets_them_so_a_second_run_is_not_a_duplicate(self) -> None:
        """Leaving the rows behind would make every restored document look like a
        duplicate of itself and land in the trash on the next run."""
        self._drop("Claim/a.txt", "a")
        run_id = self._run()

        apply_undo(self.paths, plan_undo(self.paths, run_id))
        self.assertEqual(CatalogRepository(self.paths.catalog_db_file).count_documents(), 0)

        summary = process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["duplicates"], 0, "the restored document was taken for a duplicate")

    def test_the_mirror_copy_is_quarantined_not_deleted(self) -> None:
        """Same never-delete rule as the rest of the app."""
        self._drop("Claim/a.txt", "a")
        run_id = self._run()
        mirrored = [p for p in self.paths.mirror_root.rglob("*") if p.is_file()]
        self.assertEqual(len(mirrored), 1, "setup failed: nothing was mirrored")

        report = apply_undo(self.paths, plan_undo(self.paths, run_id))

        self.assertEqual(report.mirrors_quarantined, 1)
        rescued = [p for p in self.paths.mirror_trash_dir.rglob("*") if p.is_file()]
        self.assertEqual(len(rescued), 1, "the mirror copy was deleted instead of quarantined")

    def test_a_second_undo_of_the_same_run_does_nothing(self) -> None:
        """An interrupted undo is resumed by running it again, so it must be safe
        to run twice."""
        self._drop("Claim/a.txt", "a")
        run_id = self._run()
        apply_undo(self.paths, plan_undo(self.paths, run_id))
        before = self._inbox_relatives()

        second = apply_undo(self.paths, plan_undo(self.paths, run_id))

        self.assertEqual(second.restored, 0)
        self.assertEqual(self._inbox_relatives(), before, "a second undo moved something")


class TestItRefusesRatherThanGuesses(_Workspace):
    def test_a_document_moved_by_hand_since_the_run_is_left_alone(self) -> None:
        self._drop("Claim/a.txt", "a")
        run_id = self._run()
        filed = self._library_files()[0]
        moved_to = self.paths.library_root / "Personal" / f"MOVED-{filed.name}"
        moved_to.parent.mkdir(parents=True, exist_ok=True)
        filed.rename(moved_to)

        plan = plan_undo(self.paths, run_id)
        report = apply_undo(self.paths, plan)

        self.assertEqual(report.restored, 0)
        self.assertTrue(moved_to.is_file(), "a hand-moved document was disturbed")
        self.assertTrue(plan.blocked, "the change was not reported")

    def test_a_document_renamed_since_the_run_is_left_alone(self) -> None:
        """The catalog is the authority on where a document is. A path it does not
        know must not be dragged back on the strength of a stale log line."""
        self._drop("Claim/a.txt", "a")
        run_id = self._run()
        filed = self._library_files()[0]
        filed.rename(filed.with_name("my-own-name.txt"))

        report = apply_undo(self.paths, plan_undo(self.paths, run_id))

        self.assertEqual(report.restored, 0)
        self.assertTrue(report.blocked)
        self.assertEqual(len(self._library_files()), 1, "the renamed document was moved")

    def test_a_document_the_catalog_no_longer_knows_is_left_alone(self) -> None:
        """The file is still exactly where the run put it, but the catalog has lost
        it — a rebuilt catalog, a restored snapshot, a `verify-catalog --rebuild`.
        Undo must not move a document it cannot account for: the catalog, not the
        log, is the authority on what is in the library."""
        self._drop("Claim/a.txt", "a")
        run_id = self._run()
        filed = self._library_files()[0]

        catalog = CatalogRepository(self.paths.catalog_db_file)
        row = catalog.find_by_current_path(str(filed))
        assert row is not None and row.get("doc_id")
        catalog.purge_document(str(row["doc_id"]))

        plan = plan_undo(self.paths, run_id)
        report = apply_undo(self.paths, plan)

        self.assertEqual(report.restored, 0)
        self.assertTrue(filed.is_file(), "a document the catalog had lost was moved anyway")
        self.assertTrue(
            any("moved or renamed" in reason for reason in plan.blocked), plan.blocked
        )

    def test_a_name_already_taken_in_the_inbox_is_not_overwritten(self) -> None:
        """The user may have dropped a new file of the same name in the meantime."""
        self._drop("Claim/a.txt", "original")
        run_id = self._run()
        self._drop("Claim/a.txt", "a NEWER file the user just dropped")

        apply_undo(self.paths, plan_undo(self.paths, run_id))

        claim = sorted((self.paths.inbox_dir / "Claim").iterdir())
        self.assertEqual(len(claim), 2, "the restored document overwrote the new one")
        self.assertIn(
            "a NEWER file the user just dropped",
            [p.read_text(encoding="utf-8") for p in claim],
        )

    def test_it_never_restores_outside_the_inbox(self) -> None:
        """A tampered or corrupted log must not become a way to write anywhere."""
        self._drop("Claim/a.txt", "a")
        run_id = self._run()
        escaped = str(Path(self.tmp.name) / "ELSEWHERE" / "a.txt")
        with self.paths.actions_log_file.open("a", encoding="utf-8") as handle:
            handle.write("")
        raw = self.paths.actions_log_file.read_text(encoding="utf-8")
        # Rewrite the recorded origin to a path outside the Inbox.
        original = str(self.paths.inbox_dir / "Claim" / "a.txt")
        self.paths.actions_log_file.write_text(raw.replace(original, escaped), encoding="utf-8")

        plan = plan_undo(self.paths, run_id)

        self.assertEqual(plan.restore, [])
        self.assertTrue(any("outside the Inbox" in reason for reason in plan.blocked))
        self.assertFalse(Path(escaped).exists())


class TestTheSidecar(_Workspace):
    def test_the_cached_text_is_dropped_and_never_lands_in_the_inbox(self) -> None:
        """The sidecar is a cache of what the AI read. It must not follow the
        document back: the Inbox scan lists every file, so a sidecar there would be
        ingested as a document of its own on the next run."""
        (self.paths.inbox_dir / "Claim").mkdir(parents=True, exist_ok=True)
        (self.paths.inbox_dir / "Claim" / "scan.pdf").write_bytes(b"%PDF-1.4 fake scan")
        ocr = type("R", (), {"text": "OCR TEXT", "provider": "p", "model": "m",
                             "reason": None, "is_document": False})()
        with patch.object(pipeline, "read_with_ocr", return_value=ocr) as reader:
            process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertTrue(reader.called, "the OCR path was never taken — the test proves nothing")
        run_id = latest_run_id(self.paths)
        assert run_id is not None
        sidecars = [p for p in self.paths.library_root.rglob(".*.txt")]
        self.assertEqual(len(sidecars), 1, "setup failed: no sidecar was written")

        report = apply_undo(self.paths, plan_undo(self.paths, run_id))

        self.assertEqual(report.sidecars_dropped, 1)
        self.assertEqual(
            [p.name for p in self.paths.inbox_dir.rglob("*") if p.is_file()], ["scan.pdf"],
            "the sidecar followed the document into the Inbox",
        )
        # …and it is not left orphaned in the library either, pointing at a
        # document that is no longer there.
        self.assertEqual(
            [str(p) for p in self.paths.library_root.rglob(".*.txt")], [],
            "the sidecar was orphaned in the library",
        )


class TestRunIdentification(_Workspace):
    def test_each_run_gets_its_own_id(self) -> None:
        self._drop("a.txt", "a")
        first = self._run()
        self._drop("b.txt", "b")
        second = self._run()
        self.assertNotEqual(first, second)

    def test_undoing_one_run_leaves_the_other_alone(self) -> None:
        self._drop("first.txt", "a")
        first = self._run()
        self._drop("second.txt", "b")
        self._run()
        self.assertEqual(len(self._library_files()), 2)

        apply_undo(self.paths, plan_undo(self.paths, first))

        remaining = self._library_files()
        self.assertEqual(len(remaining), 1, "the wrong run was undone")
        self.assertIn("second", remaining[0].name)

    def test_recent_runs_are_listable_with_what_they_filed(self) -> None:
        self._drop("Claim/a.txt", "a")
        self._drop("Claim/b.txt", "b")
        self._run()
        runs = list_runs(self.paths)
        self.assertEqual(len(runs), 1)
        _identifier, _when, filed = runs[0]
        self.assertEqual(filed, 2)

    def test_an_unknown_run_id_undoes_nothing(self) -> None:
        self._drop("a.txt", "a")
        self._run()
        plan = plan_undo(self.paths, "not-a-real-run-id")
        self.assertTrue(plan.is_empty)
        self.assertTrue(plan.blocked)

    def test_no_run_recorded_yet_reports_none(self) -> None:
        self.assertIsNone(latest_run_id(self.paths))


if __name__ == "__main__":
    unittest.main()
