# pyright: reportUnknownVariableType=false
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.catalog import CatalogRepository
from procrafiler.cli import main
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.flow import InvalidTransition
from procrafiler.pipeline import (
    LibraryTrashError,
    move_library_file_to_trash,
    process_next_inbox_file,
)


class TestLibraryTrash(unittest.TestCase):
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
        self.tmp.cleanup()

    def _ingest_one_library_file(self) -> Path:
        """Drop a file in the inbox and run the pipeline so it lands in the library."""
        (self.paths.inbox_dir / "doc.pdf").write_bytes(b"hello-world")
        status = process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(status, "LIBRARY_STORED")
        library_files = [p for p in self.paths.library_root.rglob("*") if p.is_file()]
        self.assertEqual(len(library_files), 1)
        return library_files[0]

    def test_happy_path_moves_file_and_quarantines_mirror(self) -> None:
        library_file = self._ingest_one_library_file()
        relative_path = library_file.relative_to(self.paths.library_root)
        mirror_file = self.paths.mirror_root / relative_path
        self.assertTrue(mirror_file.exists(), "test precondition: mirror should be in place")

        final_state = move_library_file_to_trash(self.paths, library_file, now_utc=self.now)
        self.assertEqual(final_state, "LIBRARY_TRASHED")

        # File no longer in library.
        self.assertFalse(library_file.exists())
        # File now in library_trash_manual_dir at the same relative path.
        trash_target = self.paths.library_trash_manual_dir / relative_path
        self.assertTrue(trash_target.exists())
        # Mirror copy also quarantined.
        self.assertFalse(mirror_file.exists())
        mirror_trash_target = self.paths.mirror_trash_dir / relative_path
        self.assertTrue(mirror_trash_target.exists())

        # Catalog row updated.
        repo = CatalogRepository(self.paths.catalog_db_file)
        record = repo.find_by_current_path(str(trash_target))
        self.assertIsNotNone(record)
        assert record is not None  # for type narrowing
        self.assertEqual(record["status"], "LIBRARY_TRASHED")
        self.assertEqual(record["flow_state"], "LIBRARY_TRASHED")

    def test_tolerates_missing_mirror_copy(self) -> None:
        library_file = self._ingest_one_library_file()
        relative_path = library_file.relative_to(self.paths.library_root)
        (self.paths.mirror_root / relative_path).unlink()

        final_state = move_library_file_to_trash(self.paths, library_file, now_utc=self.now)
        self.assertEqual(final_state, "LIBRARY_TRASHED")

    def test_trash_command_sends_each_sidecar_to_its_own_trash(self) -> None:
        # A trashed document drags BOTH its hidden text sidecars along, each to
        # its own library's trash: the primary sidecar → Library_Trash (with the
        # document), the mirror sidecar → Mirror_Trash (with the mirror copy).
        from procrafiler.pipeline import _sidecar_path

        rel = Path("Personal") / "2026-04-02_10-00-00__doc.pdf"
        doc = self.paths.library_root / rel
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"content")
        mirror_doc = self.paths.mirror_root / rel
        mirror_doc.parent.mkdir(parents=True, exist_ok=True)
        mirror_doc.write_bytes(b"content")
        _sidecar_path(doc).write_text("ocr text", encoding="utf-8")
        _sidecar_path(mirror_doc).write_text("ocr text", encoding="utf-8")

        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        repo.upsert_document(
            doc_id="d1", sha256="abc", current_filename=doc.name, current_path=str(doc),
            status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z", flow_state="LIBRARY_STORED",
        )
        move_library_file_to_trash(self.paths, doc, now_utc=self.now)

        sidecar_name = "." + doc.name + ".txt"
        # Primary document + primary sidecar → Library_Trash.
        lib_trash = {p.name for p in self.paths.library_trash_manual_dir.rglob("*") if p.is_file()}
        self.assertIn(doc.name, lib_trash)
        self.assertIn(sidecar_name, lib_trash)
        # Mirror copy + mirror sidecar → Mirror_Trash.
        mir_trash = {p.name for p in self.paths.mirror_trash_dir.rglob("*") if p.is_file()}
        self.assertIn(doc.name, mir_trash)
        self.assertIn(sidecar_name, mir_trash)
        # Nothing left behind at the original locations.
        self.assertFalse(_sidecar_path(doc).exists())
        self.assertFalse(_sidecar_path(mirror_doc).exists())

    def test_refuses_path_outside_library_root(self) -> None:
        stray = self.paths.inbox_dir / "stray.pdf"
        stray.write_bytes(b"x")
        with self.assertRaises(LibraryTrashError) as ctx:
            move_library_file_to_trash(self.paths, stray, now_utc=self.now)
        self.assertIn("not under library_root", str(ctx.exception))

    def test_refuses_nonexistent_file(self) -> None:
        ghost = self.paths.library_root / "Personal" / "Administrative" / "ghost.pdf"
        with self.assertRaises(LibraryTrashError) as ctx:
            move_library_file_to_trash(self.paths, ghost, now_utc=self.now)
        self.assertIn("missing", str(ctx.exception))

    def test_refuses_uncatalogued_library_file(self) -> None:
        stray = self.paths.library_root / "Personal" / "Administrative" / "stray.pdf"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"hand-placed")
        with self.assertRaises(LibraryTrashError) as ctx:
            move_library_file_to_trash(self.paths, stray, now_utc=self.now)
        self.assertIn("no catalog entry", str(ctx.exception))

    def test_double_trash_raises_invalid_transition(self) -> None:
        # First trash succeeds; the record is now in LIBRARY_TRASHED state. Trying
        # to trash the file again (from its new path in Library_Trash_Manual) is
        # rejected because LIBRARY_TRASHED has no transition back to itself, AND
        # the path is no longer under library_root anyway.
        library_file = self._ingest_one_library_file()
        move_library_file_to_trash(self.paths, library_file, now_utc=self.now)
        with self.assertRaises(LibraryTrashError):
            # Library_Trash_Manual is outside library_root → guarded by the first check.
            move_library_file_to_trash(
                self.paths,
                self.paths.library_trash_manual_dir / library_file.name,
                now_utc=self.now,
            )

    def test_invalid_transition_when_record_in_terminal_state(self) -> None:
        # Directly poison the catalog with a record at INBOX_TRASH_PENDING_MANUAL
        # (which can't transition to LIBRARY_TRASHED) but place the file in the
        # library to bypass the path check. Confirms the state-machine guard
        # actually fires.
        library_file = self._ingest_one_library_file()
        repo = CatalogRepository(self.paths.catalog_db_file)
        record = repo.find_by_current_path(str(library_file))
        assert record is not None
        repo.upsert_document(
            doc_id=str(record["doc_id"]),
            sha256=str(record["sha256"]),
            current_filename=str(record["current_filename"]),
            current_path=str(record["current_path"]),
            status="INBOX_TRASH_PENDING_MANUAL",
            updated_at_utc="2026-04-02T10:00:00Z",
            flow_state="INBOX_TRASH_PENDING_MANUAL",
        )
        with self.assertRaises(InvalidTransition):
            move_library_file_to_trash(self.paths, library_file, now_utc=self.now)

    def test_cli_library_trash_command(self) -> None:
        library_file = self._ingest_one_library_file()

        os.environ["PROCRAFILER_FAKE_NOW"] = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["library-trash", str(library_file)])
        finally:
            os.environ.pop("PROCRAFILER_FAKE_NOW", None)

        self.assertEqual(code, 0)
        self.assertIn("LIBRARY_TRASHED", stdout.getvalue())
        self.assertFalse(library_file.exists())

    def test_cli_library_trash_returns_1_on_invalid_path(self) -> None:
        outside = self.paths.inbox_dir / "doc.pdf"
        outside.write_bytes(b"x")
        stderr = io.StringIO()
        stdout = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(stdout):
            code = main(["library-trash", str(outside)])
        self.assertEqual(code, 1)
        self.assertIn("not under library_root", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
