"""P3 (deep) — durability edge cases: backup limits, restore re-rooting, mirror TTL.

These harden the corners the headline tests don't reach: an empty-library backup,
a *corrupted* encrypted archive (bit rot, not a wrong passphrase), re-rooting a
snapshot that mixes tombstones and out-of-library paths, and the mirror's
quarantine / TTL-purge boundaries. All offline, no AI.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from procrafiler.backup import create_backup, restore_from_archive
from procrafiler.catalog import CatalogRepository
from procrafiler.catalog_verify import _reroot, rebuild_catalog_from_snapshot
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.mirror import purge_mirror_trash, sync_library_file_to_mirror

_NOW_ISO = "2026-06-26T12:00:00+00:00"


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
        self.tmp_path = tmp
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.catalog = CatalogRepository(self.paths.catalog_db_file)
        self.catalog.init_schema()

    def tearDown(self) -> None:
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_")]:
            del os.environ[k]
        os.environ.update(self._snapshot)
        self._tmp.cleanup()


class TestBackupEdgeCases(_Env):
    def test_empty_library_backup_and_restore(self) -> None:
        # A brand-new, never-filed workspace: backup must still produce a valid
        # (snapshot-only) archive, and restoring it must not crash.
        dest = self.tmp_path / "backups"
        report = create_backup(self.paths, dest, now_utc=_NOW_ISO)
        self.assertEqual(report.files, 0)
        self.assertEqual(report.documents, 0)
        archive = Path(report.archive)
        self.assertTrue(archive.is_file())

        restored = restore_from_archive(self.paths, archive, now_utc=_NOW_ISO)
        self.assertEqual(restored.documents_restored, 0)
        self.assertEqual(restored.files_copied, 0)

    def test_corrupted_encrypted_archive_raises_clean_error(self) -> None:
        # Bit rot / a tampered ciphertext (header intact, wrong tag) must surface as
        # a clean ValueError — not a raw crypto exception or a traceback.
        dest = self.tmp_path / "backups"
        report = create_backup(self.paths, dest, now_utc=_NOW_ISO, passphrase="correct horse")
        enc = Path(report.archive)
        self.assertTrue(enc.name.endswith(".enc"))

        blob = bytearray(enc.read_bytes())
        blob[-1] ^= 0xFF  # flip the last byte (GCM tag) — magic header untouched
        enc.write_bytes(bytes(blob))

        with self.assertRaises(ValueError) as ctx:
            restore_from_archive(self.paths, enc, now_utc=_NOW_ISO, passphrase="correct horse")
        self.assertIn("corrupted", str(ctx.exception).lower())


class TestRestoreRerootEdgeCases(_Env):
    def test_reroot_branches(self) -> None:
        old = Path("/old/Library")
        new = Path("/new/Place")
        # under old root → moved, keeping the relative part
        self.assertEqual(_reroot("/old/Library/Personal/a.txt", old, new), str(new / "Personal/a.txt"))
        # tombstone (empty path) → unchanged
        self.assertEqual(_reroot("", old, new), "")
        # outside the old root → left as-is
        self.assertEqual(_reroot("/somewhere/else/x.txt", old, new), "/somewhere/else/x.txt")

    def test_rebuild_reroots_only_in_root_paths(self) -> None:
        old_root = Path("/old/Library")
        docs = [
            {"doc_id": "live", "sha256": "a", "current_filename": "a.txt",
             "current_path": "/old/Library/Personal/a.txt", "status": "LIBRARY_STORED",
             "updated_at_utc": "2026-01-01T00:00:00Z"},
            {"doc_id": "tomb", "sha256": "b", "current_filename": "",
             "current_path": "", "status": "DELETED", "updated_at_utc": "2026-01-02T00:00:00Z"},
            {"doc_id": "stray", "sha256": "c", "current_filename": "x.txt",
             "current_path": "/somewhere/else/x.txt", "status": "LIBRARY_STORED",
             "updated_at_utc": "2026-01-03T00:00:00Z"},
        ]
        count, _ = rebuild_catalog_from_snapshot(
            self.paths, docs, now_utc=_NOW_ISO, reroot=(old_root, self.paths.library_root)
        )
        self.assertEqual(count, 3)
        by_id = {d["doc_id"]: d["current_path"] for d in CatalogRepository(self.paths.catalog_db_file).list_documents()}
        self.assertEqual(by_id["live"], str(self.paths.library_root / "Personal/a.txt"))  # re-rooted
        self.assertEqual(by_id["tomb"], "")                                                # tombstone preserved
        self.assertEqual(by_id["stray"], "/somewhere/else/x.txt")                          # out-of-root untouched


class TestMirrorEdgeCases(_Env):
    def _lib_file(self, rel: str, content: bytes) -> Path:
        p = self.paths.library_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def _trash_files(self) -> list[Path]:
        return [p for p in self.paths.mirror_trash_dir.rglob("*") if p.is_file()]

    def test_resync_identical_content_does_not_quarantine(self) -> None:
        lib = self._lib_file("Personal/a.txt", b"same bytes")
        first = sync_library_file_to_mirror(self.paths, lib)
        self.assertTrue(first.success)
        second = sync_library_file_to_mirror(self.paths, lib)  # unchanged content
        self.assertTrue(second.success)
        self.assertIsNone(second.quarantined_path)   # nothing to version
        self.assertEqual(self._trash_files(), [])     # mirror trash stays empty

    def test_changed_content_quarantines_previous_version(self) -> None:
        lib = self._lib_file("Personal/a.txt", b"v1")
        self.assertTrue(sync_library_file_to_mirror(self.paths, lib).success)
        lib.write_bytes(b"v2 different")             # the library file changed
        result = sync_library_file_to_mirror(self.paths, lib)
        self.assertTrue(result.success)
        self.assertIsNotNone(result.quarantined_path)             # old version kept
        self.assertEqual((self.paths.mirror_root / "Personal/a.txt").read_bytes(), b"v2 different")

    def test_sync_rejects_missing_source_and_outside_root(self) -> None:
        missing = sync_library_file_to_mirror(self.paths, self.paths.library_root / "nope.txt")
        self.assertFalse(missing.success)
        self.assertEqual(missing.error, "source_missing")

        outside = self.tmp_path / "outside.txt"
        outside.write_bytes(b"x")
        res = sync_library_file_to_mirror(self.paths, outside)
        self.assertFalse(res.success)
        self.assertEqual(res.error, "outside_library_root")

    def test_purge_keeps_recent_removes_old_and_cleans_empty_dirs(self) -> None:
        now = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
        old_dir = self.paths.mirror_trash_dir / "Personal"
        old_dir.mkdir(parents=True, exist_ok=True)
        old = old_dir / "old__quarantined.txt"
        old.write_bytes(b"old")
        recent = self.paths.mirror_trash_dir / "recent__quarantined.txt"
        recent.write_bytes(b"recent")
        # old is 100 days back (past the 30-day cutoff), recent is 1 day back.
        _set_mtime(old, now - timedelta(days=100))
        _set_mtime(recent, now - timedelta(days=1))

        removed = purge_mirror_trash(self.paths, retention_days=30, now_utc=now)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())          # past the TTL → gone
        self.assertTrue(recent.exists())         # within the TTL → kept
        self.assertFalse(old_dir.exists())       # emptied directory cleaned up


def _set_mtime(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


if __name__ == "__main__":
    unittest.main()
