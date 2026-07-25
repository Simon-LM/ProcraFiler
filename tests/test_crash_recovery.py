# pyright: reportUnknownVariableType=false
"""Crash / interruption durability — the "no file is ever lost or corrupted" gate.

Item A of `docs/pre-prod-hardening.md`. A file leaves the Inbox for the Queue
BEFORE the (slow) AI read, so any hard stop can strand documents in a folder no
other code path revisits. Before this suite existed, the app reported a clean run
and `doctor` reported 0 FAIL while the user's files sat invisible in the Queue.

The central test here is the CONSERVATION INVARIANT, parameterised over every
interruption point rather than one hand-picked spot: for N inputs, after the
interrupt AND after the next run, every input is accounted for exactly once —
never zero, never twice. Plus content integrity: what gets filed must be
byte-identical to what was dropped.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import procrafiler.pipeline as pipeline
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.doctor import STATUS_FAIL, STATUS_OK, check_queue, overall_exit_code, run_doctor
from procrafiler.mirror import MIRROR_STAGING_PREFIX
from procrafiler.pipeline import (
    _move_atomically,
    _sweep_staging_files,
    process_all_inbox_files,
    recover_queue,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _WorkspaceCase(unittest.TestCase):
    """Isolated temp workspace, exactly like the rest of the suite (offline)."""

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
        self.now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- helpers ---------------------------------------------------------

    def _drop(self, name: str, body: bytes, *, folder: str | None = None) -> Path:
        target_dir = self.paths.inbox_dir / folder if folder else self.paths.inbox_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        path.write_bytes(body)
        return path

    def _all_files(self, root: Path) -> list[Path]:
        return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))

    def _locations(self) -> dict[str, list[str]]:
        """Every place a document may legitimately be, by name."""
        return {
            "inbox": [p.name for p in self._all_files(self.paths.inbox_dir)],
            "queue": [p.name for p in self._all_files(self.paths.queue_dir)],
            "library": [p.name for p in self._all_files(self.paths.library_root)],
            "inbox_trash": [p.name for p in self._all_files(self.paths.inbox_trash_manual_dir)],
        }

    def _accounted(self) -> list[str]:
        """All document basenames currently accounted for anywhere."""
        return [name for names in self._locations().values() for name in names]


class TestQueueRecovery(_WorkspaceCase):
    def test_interrupt_strands_files_and_next_run_recovers_them(self) -> None:
        """The exact production scenario: Ctrl-C mid-batch must not lose files."""
        for i in range(3):
            self._drop(f"scan_{i}.txt", f"claim page {i}".encode(), folder="Water-Damage")

        calls = {"n": 0}
        original = pipeline._read_and_analyze

        def interrupt_on_third(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt()
            return original(*args, **kwargs)

        with patch.object(pipeline, "_read_and_analyze", interrupt_on_third):
            with self.assertRaises(KeyboardInterrupt):
                process_all_inbox_files(self.paths, now_utc=self.now)

        # Interrupted: the files are stranded in the Queue (the bug's symptom).
        self.assertEqual(len(self._all_files(self.paths.queue_dir)), 3)
        self.assertEqual(len(self._all_files(self.paths.inbox_dir)), 0)

        # The next run must bring them back and process them — nothing left behind.
        summary = process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(summary["recovered"], 3)
        self.assertEqual(self._all_files(self.paths.queue_dir), [])
        self.assertEqual(summary["total"], 3)

    def test_recovery_restores_the_inbox_subfolder_so_sets_survive(self) -> None:
        """Files dropped together are a SET; recovery must not scatter them to the
        Inbox root, which would silently destroy the grouping the user intended."""
        self._drop("a.txt", b"page a", folder="Water-Damage")
        self._drop("b.txt", b"page b", folder="Water-Damage")

        # Phase 1 catalogs the whole set before anything is filed, and the move to
        # the Queue precedes the read — so interrupting the SECOND read strands both.
        calls = {"n": 0}
        original = pipeline._read_and_analyze

        def interrupt_on_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt()
            return original(*args, **kwargs)

        with patch.object(pipeline, "_read_and_analyze", interrupt_on_second):
            with self.assertRaises(KeyboardInterrupt):
                process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(len(self._all_files(self.paths.queue_dir)), 2)

        recovered = recover_queue(
            self.paths, now_utc=self.now, features={"actions_log": True}, emit=lambda _m: None
        )
        self.assertEqual(recovered, 2)
        restored = sorted(
            str(p.relative_to(self.paths.inbox_dir)) for p in self._all_files(self.paths.inbox_dir)
        )
        self.assertEqual(restored, ["Water-Damage/a.txt", "Water-Damage/b.txt"])

    def test_recovery_falls_back_to_inbox_root_without_an_action_log(self) -> None:
        """With actions_log disabled there is no origin record. The file must still
        be recovered (to the root) — never left stranded."""
        self._drop("orphan.txt", b"body")
        with patch.object(pipeline, "_read_and_analyze", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                process_all_inbox_files(self.paths, now_utc=self.now)
        self.paths.actions_log_file.unlink()

        recovered = recover_queue(
            self.paths, now_utc=self.now, features={"actions_log": False}, emit=lambda _m: None
        )
        self.assertEqual(recovered, 1)
        self.assertEqual([p.name for p in self._all_files(self.paths.inbox_dir)], ["orphan.txt"])

    def test_recovery_is_idempotent_when_interrupted_during_recovery(self) -> None:
        """A crash DURING recovery must leave the rest for the next run, not lose it."""
        # Start from the post-crash state directly: several documents stranded in
        # the Queue is exactly what a hard kill leaves behind.
        for i in range(4):
            (self.paths.queue_dir / f"f{i}.txt").write_bytes(f"body {i}".encode())
        stranded = len(self._all_files(self.paths.queue_dir))
        self.assertEqual(stranded, 4)

        # Interrupt the recovery itself, partway through.
        real_move = pipeline.move
        moves = {"n": 0}

        def fail_after_one(src, dst):
            moves["n"] += 1
            if moves["n"] > 1:
                raise KeyboardInterrupt()
            return real_move(src, dst)

        with patch.object(pipeline, "move", fail_after_one):
            with self.assertRaises(KeyboardInterrupt):
                recover_queue(
                    self.paths, now_utc=self.now, features={"actions_log": True}, emit=lambda _m: None
                )

        # Nothing vanished: every file is either back in the Inbox or still queued.
        self.assertEqual(
            len(self._all_files(self.paths.inbox_dir)) + len(self._all_files(self.paths.queue_dir)),
            stranded,
        )
        # And a further recovery finishes the job.
        recover_queue(self.paths, now_utc=self.now, features={"actions_log": True}, emit=lambda _m: None)
        self.assertEqual(self._all_files(self.paths.queue_dir), [])

    def test_recovery_is_a_no_op_on_a_clean_queue(self) -> None:
        self.assertEqual(
            recover_queue(
                self.paths, now_utc=self.now, features={"actions_log": True}, emit=lambda _m: None
            ),
            0,
        )

    def test_a_queued_duplicate_is_trashed_not_double_filed(self) -> None:
        """Recovery must not create a second copy of a document already filed."""
        body = b"identical content"
        self._drop("first.txt", body)
        process_all_inbox_files(self.paths, now_utc=self.now)
        filed_before = len(self._all_files(self.paths.library_root))

        # Same content stranded in the Queue by an interrupted later run.
        (self.paths.queue_dir / "second.txt").write_bytes(body)
        summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(len(self._all_files(self.paths.library_root)), filed_before)
        self.assertEqual([p.name for p in self._all_files(self.paths.inbox_trash_manual_dir)], ["second.txt"])


class TestConservationInvariant(_WorkspaceCase):
    """For N inputs, after an interrupt at ANY step and after the next run, every
    input is accounted for exactly once. This is the test that must fail if anyone
    reintroduces a path where a document can go missing."""

    STEPS = ("_read_and_analyze", "_route_from_analysis", "_file_cataloged")

    def test_no_file_is_ever_lost_whatever_the_interruption_point(self) -> None:
        for step in self.STEPS:
            for fail_at in (1, 2, 3):
                with self.subTest(step=step, fail_at=fail_at):
                    self._run_one(step, fail_at)

    def _run_one(self, step: str, fail_at: int) -> None:
        self.tearDown()
        self.setUp()
        inputs = {}
        for i in range(3):
            path = self._drop(f"doc_{i}.txt", f"document body number {i}".encode())
            inputs[f"doc_{i}.txt"] = _sha256(path)

        calls = {"n": 0}
        original = getattr(pipeline, step)

        def interrupt(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise KeyboardInterrupt()
            return original(*args, **kwargs)

        with patch.object(pipeline, step, interrupt):
            try:
                process_all_inbox_files(self.paths, now_utc=self.now)
            except KeyboardInterrupt:
                pass

        # Invariant 1 — nothing vanished at the moment of the interrupt.
        accounted = self._accounted()
        self.assertEqual(
            len(accounted),
            3,
            f"{step}@{fail_at}: {len(accounted)} of 3 files accounted for: {self._locations()}",
        )

        # Invariant 2 — the next run settles everything, still without loss.
        process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(
            self._all_files(self.paths.queue_dir),
            [],
            f"{step}@{fail_at}: files left stranded in the Queue after a follow-up run",
        )
        settled = self._accounted()
        self.assertEqual(
            len(settled), 3, f"{step}@{fail_at}: {len(settled)} of 3 after recovery: {self._locations()}"
        )

        # Invariant 3 — CONTENT integrity, not just presence: every original
        # content hash is still on disk somewhere (a count check cannot catch a
        # truncated or half-copied file).
        on_disk = {
            _sha256(p)
            for root in (
                self.paths.inbox_dir,
                self.paths.queue_dir,
                self.paths.library_root,
                self.paths.inbox_trash_manual_dir,
            )
            for p in self._all_files(root)
        }
        for name, digest in inputs.items():
            self.assertIn(digest, on_disk, f"{step}@{fail_at}: content of {name} corrupted or lost")


class TestAtomicPlacement(_WorkspaceCase):
    """A cross-filesystem move degrades to copy+unlink, so an interrupted placement
    could leave a TRUNCATED document at its final library path — which `rescan`
    would then ingest as a genuine new file. Placement must be atomic."""

    def test_interrupted_placement_leaves_no_partial_document(self) -> None:
        target_dir = self.paths.library_root / "Personal"
        target_dir.mkdir(parents=True, exist_ok=True)
        source = self.paths.queue_dir / "doc.txt"
        source.write_bytes(b"the full and complete document body")
        target = target_dir / "2026-07-25_12-00-00__Doc.txt"

        # Simulate a kill during the copy phase of the staged move.
        with patch.object(pipeline, "move", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                _move_atomically(source, target)

        # The real path must NOT exist half-written.
        self.assertFalse(target.exists(), "a partial document appeared at its final library path")
        # The source is untouched, so nothing was lost.
        self.assertTrue(source.is_file())
        self.assertEqual(source.read_bytes(), b"the full and complete document body")

    def test_successful_placement_is_byte_exact_and_leaves_no_staging(self) -> None:
        target_dir = self.paths.library_root / "Personal"
        target_dir.mkdir(parents=True, exist_ok=True)
        source = self.paths.queue_dir / "doc.txt"
        body = b"payload" * 5000
        source.write_bytes(body)
        target = target_dir / "filed.txt"

        _move_atomically(source, target)

        self.assertEqual(target.read_bytes(), body)
        self.assertFalse(source.exists())
        self.assertEqual(list(target_dir.glob(".procrafiler-incoming__*")), [])

    def test_staging_leftovers_are_swept_and_real_documents_are_not(self) -> None:
        target_dir = self.paths.library_root / "Personal"
        target_dir.mkdir(parents=True, exist_ok=True)
        leftover = target_dir / ".procrafiler-incoming__deadbeef.tmp"
        leftover.write_bytes(b"incomplete")
        keeper = target_dir / "2026-07-25_12-00-00__Real.txt"
        keeper.write_bytes(b"a real filed document")

        removed = _sweep_staging_files(self.paths.library_root)

        self.assertEqual(removed, 1)
        self.assertFalse(leftover.exists())
        self.assertTrue(keeper.is_file(), "the sweep must never touch a real document")

    def test_the_pipeline_actually_uses_atomic_placement(self) -> None:
        """Wiring test: the helper being correct is useless if `_file_cataloged`
        does not use it. Simulate a partial cross-filesystem copy (the real failure
        mode: `shutil.move` degrades to copy+unlink) and assert no truncated file
        is ever exposed under a real document name in the library."""
        body = b"the complete document body, all of it" * 100
        self._drop("doc.txt", body)

        real_move = pipeline.move
        library_bound: list[str] = []

        def half_copy_on_library_move(src, dst):
            # Only sabotage the move INTO the library — sabotaging the earlier
            # Inbox→Queue move would abort before placement and make this test
            # pass vacuously (it did, on the first attempt).
            if Path(dst).is_relative_to(self.paths.library_root):
                library_bound.append(str(dst))
                Path(dst).write_bytes(Path(src).read_bytes()[: len(body) // 2])
                raise KeyboardInterrupt()
            return real_move(src, dst)

        with patch.object(pipeline, "move", half_copy_on_library_move):
            with self.assertRaises(KeyboardInterrupt):
                process_all_inbox_files(self.paths, now_utc=self.now)

        # Anti-vacuity guard: prove the library placement was really exercised.
        self.assertEqual(len(library_bound), 1, "the library placement was never reached")

        # The interrupted copy must NOT be visible under a real document name: a
        # truncated file exposed here would be ingested by the next `rescan` as a
        # genuine new document — silent corruption. Only a hidden staging file may
        # remain, and the sweep clears it.
        visible = self._all_files(self.paths.library_root)
        self.assertEqual(
            visible, [], f"a truncated document is exposed in the library: {[p.name for p in visible]}"
        )
        self.assertTrue(
            Path(library_bound[0]).name.startswith(".procrafiler-incoming__"),
            "the placement wrote straight to the final path instead of staging",
        )
        _sweep_staging_files(self.paths.library_root)
        self.assertEqual(list(self.paths.library_root.rglob(".procrafiler-incoming__*")), [])

    def test_mirror_staging_leftovers_are_swept_too(self) -> None:
        """A hard kill during the mirror sync leaves a mirror staging file. It is
        hidden and harmless, but it must not accumulate run after run."""
        mirror_dir = self.paths.mirror_root / "Personal"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        leftover = mirror_dir / f"{MIRROR_STAGING_PREFIX}cafe1234.tmp"
        leftover.write_bytes(b"incomplete mirror copy")
        real_copy = mirror_dir / "2026-07-25_12-00-00__Real.txt"
        real_copy.write_bytes(b"a complete mirror copy")

        removed = _sweep_staging_files(self.paths.mirror_root)

        self.assertEqual(removed, 1)
        self.assertFalse(leftover.exists())
        self.assertTrue(real_copy.is_file(), "the sweep must never touch a real mirror copy")

    def test_a_long_filename_still_places_atomically(self) -> None:
        """The staging name must be fixed-length: prefixing a long target name could
        blow the 255-byte limit and fail the very move it protects."""
        target_dir = self.paths.library_root / "Personal"
        target_dir.mkdir(parents=True, exist_ok=True)
        source = self.paths.queue_dir / "src.txt"
        source.write_bytes(b"body")
        long_name = "2026-07-25_12-00-00__" + ("N" * 200) + ".txt"
        target = target_dir / long_name

        _move_atomically(source, target)

        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"body")


class TestDoctorSeesStrandedFiles(_WorkspaceCase):
    def test_doctor_fails_and_names_the_stranded_files(self) -> None:
        (self.paths.queue_dir / "lost_invoice.pdf").write_bytes(b"x")

        checks = check_queue(self.paths)
        self.assertEqual(checks[0].status, STATUS_FAIL)
        self.assertIn("lost_invoice.pdf", checks[0].message)

        # And the whole report must exit non-zero — no clean bill of health while
        # the user's documents are invisible.
        self.assertEqual(overall_exit_code(run_doctor(self.paths)), 1)

    def test_doctor_is_ok_on_a_clean_queue(self) -> None:
        self.assertEqual(check_queue(self.paths)[0].status, STATUS_OK)


if __name__ == "__main__":
    unittest.main()
