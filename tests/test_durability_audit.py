# pyright: reportUnknownVariableType=false
"""Durability audit — the remaining gate of docs/pre-prod-hardening.md (item G).

The bar is not coverage: it is whether any sequence of failures can make a
document disappear or change silently. What the earlier PRs already cover (the
conservation invariant, crash-during-recovery, content integrity, nested paths,
restore safety, hint weighting) is not repeated here. This file adds the rest:

- adversarial filenames surviving a full round trip, action log included;
- failure injection (ENOSPC / EACCES / EIO) at every write site;
- an interrupted mirror sync being caught and healed by `scrub`;
- the hidden text sidecar never being orphaned or mismatched.

`SIGKILL` and real two-process concurrency live in `test_durability_processes.py`
— they need real subprocesses.
"""
from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import procrafiler.pipeline as pipeline
from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _sidecar_path, process_all_inbox_files
from procrafiler.scrub import scrub


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

    def _library_files(self) -> list[Path]:
        return sorted(
            p for p in self.paths.library_root.rglob("*") if p.is_file() and not p.name.startswith(".")
        )

    def _log_events(self) -> list[dict]:
        """Every action-log line, parsed. Fails loudly if the log is not valid JSONL."""
        events = []
        for i, line in enumerate(
            self.paths.actions_log_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError as exc:
                self.fail(f"action log line {i} is not valid JSON ({exc}): {line[:120]!r}")
        return events


class TestAdversarialFilenames(_Workspace):
    """A filename is attacker- or accident-controlled data. It must not be able to
    break the filesystem write, the action log, or the catalog."""

    HOSTILE = {
        "newline": "in\nvoice.txt",
        "carriage_return": "in\rvoice.txt",
        "tab": "in\tvoice.txt",
        "quotes": 'in"voi\'ce.txt',
        "unicode": "facture_éàü_中文_🙂.txt",
        "rtl_override": "invo‮cod.txt",
        "trailing_space": "invoice .txt",
        "trailing_dot": "invoice..txt",
        "leading_dash": "--invoice.txt",
        "very_long": "A" * 200 + ".txt",
        "only_punctuation": "___---.txt",
        "semicolon_shell": "invoice; rm -rf ~.txt",
        "dollar_shell": "invoice$(whoami).txt",
    }

    def test_each_hostile_name_survives_a_full_run(self) -> None:
        for label, name in self.HOSTILE.items():
            with self.subTest(name=label):
                self.tearDown()
                self.setUp()
                body = f"content for {label}".encode()
                try:
                    (self.paths.inbox_dir / name).write_bytes(body)
                except OSError:
                    self.skipTest(f"the filesystem itself refuses {label!r}")

                process_all_inbox_files(self.paths, now_utc=self.now)

                # The document is filed, complete, and under a sane name.
                filed = self._library_files()
                self.assertEqual(len(filed), 1, f"{label}: expected exactly one filed document")
                self.assertEqual(filed[0].read_bytes(), body, f"{label}: content changed")
                self.assertLessEqual(
                    len(filed[0].name.encode()), 255, f"{label}: name exceeds the filesystem limit"
                )
                for forbidden in ("\n", "\r", "\t", "/"):
                    self.assertNotIn(forbidden, filed[0].name, f"{label}: control char survived")

    def test_the_action_log_stays_valid_jsonl(self) -> None:
        """A newline in a filename must not split one event across two log lines —
        that would corrupt every downstream reader, including Queue recovery."""
        for name in ("in\nvoice.txt", 'quo"te.txt', "facture_éàü_🙂.txt"):
            try:
                (self.paths.inbox_dir / name).write_bytes(b"body")
            except OSError:
                continue
        process_all_inbox_files(self.paths, now_utc=self.now)

        events = self._log_events()  # fails the test if any line is not JSON
        self.assertTrue(events)
        self.assertTrue(any(e.get("action") == "move_to_library" for e in events))

    def test_a_hostile_name_is_recoverable_from_the_queue(self) -> None:
        """Queue recovery reads the action log to find where a file came from. A
        name that breaks the log would strand the document."""
        name = "in\nvoice.txt"
        try:
            (self.paths.inbox_dir / "Set").mkdir()
            (self.paths.inbox_dir / "Set" / name).write_bytes(b"body")
        except OSError:
            self.skipTest("the filesystem refuses this name")

        with patch.object(pipeline, "_read_and_analyze", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(len(list(self.paths.queue_dir.iterdir())), 1)

        summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(list(self.paths.queue_dir.iterdir()), [])


class TestSidecarCoupling(_Workspace):
    """The hidden `.txt` sidecar holds text that cost an AI call (OCR/vision). It
    must follow its document exactly — never orphaned, never pointing at another."""

    def _file_with_sidecar(self) -> Path:
        """File one document whose text came from OCR, so a sidecar is written.

        A PDF with no text layer already dispatches to OCR on its own (verified:
        `extract_text_content` returns text=None, reader_hint="ocr"), so only the
        OCR reader itself needs mocking — the suite stays offline.
        """
        (self.paths.inbox_dir / "scan.pdf").write_bytes(b"%PDF-1.4 fake scan")
        ocr = type("R", (), {"text": "OCR TEXT", "provider": "p", "model": "m", "reason": None, "is_document": False})()
        with patch.object(pipeline, "read_with_ocr", return_value=ocr) as reader:
            process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertTrue(reader.called, "the OCR path was never taken — the test proves nothing")
        filed = self._library_files()
        self.assertEqual(len(filed), 1, "setup failed: the document was not filed")
        return filed[0]

    def test_the_sidecar_sits_next_to_its_document(self) -> None:
        document = self._file_with_sidecar()
        sidecar = _sidecar_path(document)
        self.assertTrue(sidecar.is_file(), "the AI-extracted text was not cached")
        self.assertIn("OCR TEXT", sidecar.read_text(encoding="utf-8"))

    def test_a_hand_move_takes_the_sidecar_along(self) -> None:
        """Move ONLY the document, as a user would in a file manager — the sidecar
        is hidden, so they will not move it. `rescan` must bring it along, or the
        costly OCR text is orphaned and search silently loses the document's body.
        """
        document = self._file_with_sidecar()
        old_sidecar = _sidecar_path(document)
        self.assertTrue(old_sidecar.is_file(), "setup failed: no sidecar to follow")

        target_dir = self.paths.library_root / "Personal" / "Administrative" / "Banking"
        target_dir.mkdir(parents=True, exist_ok=True)
        moved = target_dir / document.name
        os.rename(document, moved)  # the document alone; the sidecar stays behind

        process_all_inbox_files(self.paths, now_utc=self.now)  # runs rescan first

        self.assertTrue(moved.is_file())
        self.assertTrue(_sidecar_path(moved).is_file(), "the sidecar did not follow its document")
        self.assertFalse(old_sidecar.exists(), "a sidecar was orphaned at the old path")

    def test_no_sidecar_is_written_for_a_mechanical_read(self) -> None:
        """Plain text needs no cache — its body is free to re-extract. A stray
        sidecar would be dead weight mirrored forever."""
        (self.paths.inbox_dir / "note.txt").write_text("plain body")
        process_all_inbox_files(self.paths, now_utc=self.now)
        filed = self._library_files()
        self.assertEqual(len(filed), 1)
        self.assertFalse(_sidecar_path(filed[0]).exists())


class TestWriteFailureInjection(_Workspace):
    """Every write site must fail SAFELY: no document lost, none left truncated
    under a real name. ENOSPC is the realistic one — a disk filling mid-run."""

    def _inject(self, err: int):
        real_rename = os.rename

        def failing_rename(a, b, *args, **kwargs):
            if Path(b).is_relative_to(self.paths.library_root):
                raise OSError(err, os.strerror(err))
            return real_rename(a, b, *args, **kwargs)

        return patch("procrafiler.pipeline.os.rename", side_effect=failing_rename)

    def test_a_failing_library_write_never_loses_the_document(self) -> None:
        for label, err in (("ENOSPC", errno.ENOSPC), ("EACCES", errno.EACCES), ("EIO", errno.EIO)):
            with self.subTest(error=label):
                self.tearDown()
                self.setUp()
                (self.paths.inbox_dir / "doc.txt").write_bytes(b"the document")

                with self._inject(err):
                    process_all_inbox_files(self.paths, now_utc=self.now)

                # It failed, so nothing is in the library…
                self.assertEqual(self._library_files(), [], f"{label}: a partial document appeared")
                # …but the document itself still exists, recoverable from the Queue.
                queued = list(self.paths.queue_dir.iterdir())
                self.assertEqual(len(queued), 1, f"{label}: the document was lost")
                self.assertEqual(queued[0].read_bytes(), b"the document", f"{label}: content changed")

    def test_the_batch_reports_the_error_instead_of_claiming_success(self) -> None:
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"the document")
        with self._inject(errno.ENOSPC):
            summary = process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(summary["errors"], 1, "a failed write was reported as a clean run")

    def test_a_failing_mirror_copy_does_not_fail_the_filing(self) -> None:
        """The mirror is a backup. Losing it must not cost the primary copy."""
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"the document")
        with patch(
            "procrafiler.mirror.copy2", side_effect=OSError(errno.ENOSPC, "No space left on device")
        ):
            summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(len(self._library_files()), 1, "the document was not filed")
        self.assertEqual(summary["mirror_failures"], 1, "the mirror failure was not reported")


class TestInterruptedMirrorIsHealed(_Workspace):
    """A mirror copy corrupted or lost after the fact must be detected by `scrub`
    and repaired from the library — that is the whole point of the durability work."""

    def _file_one(self) -> Path:
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"the authoritative content")
        process_all_inbox_files(self.paths, now_utc=self.now)
        filed = self._library_files()
        self.assertEqual(len(filed), 1)
        return filed[0]

    def _mirror_of(self, document: Path) -> Path:
        return self.paths.mirror_root / document.relative_to(self.paths.library_root)

    def test_scrub_detects_and_repairs_a_truncated_mirror_copy(self) -> None:
        document = self._file_one()
        mirror_copy = self._mirror_of(document)
        self.assertTrue(mirror_copy.is_file(), "setup failed: nothing was mirrored")
        mirror_copy.write_bytes(b"trunc")  # simulate an interrupted copy

        catalog = CatalogRepository(self.paths.catalog_db_file)
        report = scrub(self.paths, catalog, repair=False)
        self.assertFalse(report.healthy, "scrub did not notice the corrupted mirror copy")

        repaired = scrub(self.paths, catalog, repair=True)
        self.assertTrue(repaired.healthy, f"scrub did not heal it: {repaired.issues}")
        self.assertEqual(mirror_copy.read_bytes(), b"the authoritative content")

    def test_scrub_detects_and_repairs_a_missing_mirror_copy(self) -> None:
        document = self._file_one()
        mirror_copy = self._mirror_of(document)
        mirror_copy.unlink()

        catalog = CatalogRepository(self.paths.catalog_db_file)
        self.assertFalse(scrub(self.paths, catalog, repair=False).healthy)
        self.assertTrue(scrub(self.paths, catalog, repair=True).healthy)
        self.assertEqual(mirror_copy.read_bytes(), b"the authoritative content")

    def test_scrub_heals_a_corrupted_library_copy_from_the_mirror(self) -> None:
        """The direction that matters most: the primary is the one you lose."""
        document = self._file_one()
        document.write_bytes(b"bit rot")

        catalog = CatalogRepository(self.paths.catalog_db_file)
        self.assertFalse(scrub(self.paths, catalog, repair=False).healthy)
        self.assertTrue(scrub(self.paths, catalog, repair=True).healthy)
        self.assertEqual(document.read_bytes(), b"the authoritative content")

    def test_scrub_never_repairs_from_a_copy_that_is_itself_bad(self) -> None:
        """Both copies corrupt = unrecoverable. It must say so, not spread the rot."""
        document = self._file_one()
        mirror_copy = self._mirror_of(document)
        document.write_bytes(b"rot A")
        mirror_copy.write_bytes(b"rot B")

        catalog = CatalogRepository(self.paths.catalog_db_file)
        report = scrub(self.paths, catalog, repair=True)

        self.assertFalse(report.healthy)
        self.assertEqual(report.repaired, [], "scrub restored from an unverified copy")
        self.assertEqual(document.read_bytes(), b"rot A", "a bad copy was overwritten by another")


class TestFailureDuringTheRepairItself(_Workspace):
    """A repair is the last line of defence. A failure *inside* it must not make
    things worse than they already were.

    Found by mutation: the failure injection above targets the pipeline's library
    write, and nothing had ever injected a fault into `scrub --repair`. Removing
    scrub's post-copy verification, or its `OSError` handler, left the whole suite
    green — the code was right, but nothing proved it.
    """

    def _corrupted_library_copy_with_a_good_mirror(self) -> Path:
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"the authoritative content")
        process_all_inbox_files(self.paths, now_utc=self.now)
        document = self._library_files()[0]
        document.write_bytes(b"bit rot")  # only the primary is bad
        return document

    def _heal_leftovers(self) -> list[Path]:
        return [p for p in self.paths.library_root.rglob("*.heal-tmp")]

    def test_a_repair_that_writes_garbage_is_not_swapped_in(self) -> None:
        """The copy is hashed again *after* being written, before replacing the
        document. Without that check a corrupted heal would overwrite a merely
        damaged file — and be reported as repaired."""
        document = self._corrupted_library_copy_with_a_good_mirror()

        def corrupting_copy(src, dst, *args, **kwargs):  # noqa: ANN001, ANN202
            Path(dst).write_bytes(b"corrupted in transit")

        catalog = CatalogRepository(self.paths.catalog_db_file)
        with patch("procrafiler.scrub.shutil.copy2", side_effect=corrupting_copy):
            report = scrub(self.paths, catalog, repair=True)

        self.assertEqual(report.repaired, [], "a corrupted heal was reported as a repair")
        self.assertEqual(
            document.read_bytes(), b"bit rot",
            "the corrupted heal was swapped in over the damaged document",
        )
        self.assertEqual(self._heal_leftovers(), [], "a .heal-tmp file was left behind")
        self.assertFalse(report.healthy, "the document was reported healthy")

    def test_a_disk_filling_during_a_repair_reports_instead_of_crashing(self) -> None:
        """`scrub --repair` on a full disk must come back with a failed repair, not
        an OSError traceback — and must not leave a partial file next to the
        document, where the next scrub would read it."""
        document = self._corrupted_library_copy_with_a_good_mirror()

        catalog = CatalogRepository(self.paths.catalog_db_file)
        with patch(
            "procrafiler.scrub.shutil.copy2",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ):
            report = scrub(self.paths, catalog, repair=True)  # must not raise

        self.assertEqual(report.repaired, [])
        self.assertEqual(document.read_bytes(), b"bit rot", "the document changed anyway")
        self.assertEqual(self._heal_leftovers(), [], "a .heal-tmp file was left behind")
        self.assertEqual(report.corrupt, 1, "the still-corrupt document was not reported")

    def test_the_repair_still_works_when_nothing_fails(self) -> None:
        """Anti-vacuity: the two tests above must fail because of the INJECTED
        fault, not because this scenario never repairs anything."""
        document = self._corrupted_library_copy_with_a_good_mirror()
        catalog = CatalogRepository(self.paths.catalog_db_file)

        report = scrub(self.paths, catalog, repair=True)

        self.assertEqual(len(report.repaired), 1, "the scenario does not repair at all")
        self.assertEqual(document.read_bytes(), b"the authoritative content")


class TestMirrorFailurePathsCleanUpAfterThemselves(_Workspace):
    """Two mirror paths that only run when something goes wrong, and that no test
    had ever entered."""

    def _file_one(self) -> Path:
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"first version")
        process_all_inbox_files(self.paths, now_utc=self.now)
        return self._library_files()[0]

    def test_a_failed_mirror_copy_leaves_no_staging_file_behind(self) -> None:
        """The mirror stages into a hidden temp file and renames it. If the copy
        fails, that temp file must go — otherwise every failed sync leaves one more
        `.procrafiler-mirror__*.tmp` in the mirror, forever.

        The injected failure writes a **partial file and then raises**, which is
        what a disk filling mid-copy actually does. Raising before writing anything
        leaves nothing to clean up, so it cannot exercise the cleanup at all — the
        first version of this test made exactly that mistake and passed even with
        the cleanup removed.
        """
        (self.paths.inbox_dir / "doc.txt").write_bytes(b"the document")

        def fills_the_disk(src, dst, *args, **kwargs):  # noqa: ANN001, ANN202
            Path(dst).write_bytes(b"the doc")  # partial write…
            raise OSError(errno.ENOSPC, "No space left on device")  # …then no room left

        with patch("procrafiler.mirror.copy2", side_effect=fills_the_disk):
            summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["mirror_failures"], 1, "setup failed: the mirror did not fail")
        leftovers = [
            p for p in self.paths.mirror_root.rglob("*") if p.name.startswith(".procrafiler-mirror__")
        ]
        self.assertEqual(leftovers, [], f"staging files left in the mirror: {leftovers}")
        # And the truncated bytes never reached a real mirror path.
        real = [
            p for p in self.paths.mirror_root.rglob("*")
            if p.is_file() and not p.name.startswith(".") and "Trash" not in p.parts
        ]
        self.assertEqual(real, [], f"a truncated file landed at a real mirror path: {real}")

    def test_a_second_quarantine_in_the_same_second_does_not_overwrite_the_first(self) -> None:
        """When the mirror holds a copy that differs from the library, it is moved
        to the mirror trash under a timestamped name. Two quarantines of the same
        document within the same second collide on that name — and the first
        rescued version would be silently replaced by the second."""
        from procrafiler.mirror import sync_library_file_to_mirror

        document = self._file_one()
        mirror_copy = self.paths.mirror_root / document.relative_to(self.paths.library_root)
        self.assertTrue(mirror_copy.is_file(), "setup failed: nothing was mirrored")

        # Two syncs, one fixed timestamp: both quarantine names are identical.
        for stale in (b"stale version A", b"stale version B"):
            mirror_copy.write_bytes(stale)
            result = sync_library_file_to_mirror(self.paths, document, now_utc=self.now)
            self.assertTrue(result.success, f"mirror sync failed: {result.error}")

        rescued = sorted(p for p in self.paths.mirror_trash_dir.rglob("*") if p.is_file())
        self.assertEqual(len(rescued), 2, f"a quarantined copy was overwritten: {rescued}")
        self.assertEqual(
            sorted(p.read_bytes() for p in rescued), [b"stale version A", b"stale version B"]
        )


if __name__ == "__main__":
    unittest.main()
