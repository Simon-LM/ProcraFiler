from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from procrafiler.backup import (
    backup_reminder,
    create_backup,
    is_encrypted_archive,
    last_backup_utc,
    record_backup,
    restore_from_archive,
)
from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout

_NOW = "2026-06-24T12:00:00+00:00"
_DOCS = {"Personal/a.txt": b"alpha", "Work/sub/b.txt": b"beta"}


def _set_env(base: Path) -> None:
    for var, sub in (("WORKSPACE_DIR", "Inbox"), ("LIBRARY_DIR", "Library"),
                     ("LIBRARY_MIRROR_DIR", "Mirror"), ("HOME", "state"), ("CONFIG_HOME", "config")):
        os.environ[f"PROCRAFILER_{var}"] = str(base / sub)


class _BackupEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._snapshot = {k: v for k, v in os.environ.items() if k.startswith("PROCRAFILER_")}
        for k in list(os.environ):
            if k.startswith("PROCRAFILER_"):
                del os.environ[k]
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        _set_env(self.tmp / "src")
        self.src = default_runtime_paths()
        ensure_runtime_layout(self.src)
        cat = CatalogRepository(self.src.catalog_db_file)
        cat.init_schema()
        for rel, content in _DOCS.items():
            f = self.src.library_root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content)
            cat.upsert_document(
                doc_id=rel, sha256=hashlib.sha256(content).hexdigest(),
                current_filename=Path(rel).name, current_path=str(self.src.library_root / rel),
                status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00+00:00",
                content_json='{"name": "Doc"}',
            )
        self.backup_dir = self.tmp / "backups"

    def tearDown(self) -> None:
        for k in [k for k in os.environ if k.startswith("PROCRAFILER_")]:
            del os.environ[k]
        os.environ.update(self._snapshot)
        self._tmp.cleanup()


class TestBackup(_BackupEnv):
    def test_create_writes_dated_archive_and_matching_checksum(self) -> None:
        report = create_backup(self.src, self.backup_dir, now_utc=_NOW)
        archive = Path(report.archive)
        self.assertTrue(archive.is_file())
        self.assertTrue(archive.name.startswith("procrafiler-backup-"))
        self.assertEqual((report.files, report.documents), (2, 2))
        # checksum file matches the archive
        checksum = archive.with_name(archive.name + ".sha256").read_text(encoding="utf-8").split()[0]
        self.assertEqual(checksum, hashlib.sha256(archive.read_bytes()).hexdigest())
        # last-backup date recorded
        self.assertEqual(last_backup_utc(self.src), _NOW)

    def test_archive_is_mirror_shaped(self) -> None:
        report = create_backup(self.src, self.backup_dir, now_utc=_NOW)
        with tarfile.open(report.archive, "r:*") as tar:
            names = set(tar.getnames())
        self.assertIn("Personal/a.txt", names)
        self.assertIn("Work/sub/b.txt", names)
        self.assertIn(".procrafiler/catalog_snapshot.json", names)

    def test_backup_then_restore_archive_roundtrip(self) -> None:
        report = create_backup(self.src, self.backup_dir, now_utc=_NOW)
        # Restore into a fresh, different location.
        _set_env(self.tmp / "dst")
        dst = default_runtime_paths()
        ensure_runtime_layout(dst)
        restored = restore_from_archive(dst, Path(report.archive), now_utc=_NOW)
        self.assertEqual((restored.files_copied, restored.documents_restored), (2, 2))
        self.assertEqual((dst.library_root / "Personal/a.txt").read_bytes(), b"alpha")
        cat = CatalogRepository(dst.catalog_db_file)
        self.assertTrue(cat.integrity_ok())
        docs = cat.list_documents()
        self.assertTrue(all(str(d["current_path"]).startswith(str(dst.library_root)) for d in docs))
        self.assertEqual(json.loads(str(docs[0]["content_json"])), {"name": "Doc"})

    def test_reminder_never_then_fresh_then_overdue(self) -> None:
        self.assertIn("No offline backup yet", str(backup_reminder(self.src, now_utc=_NOW)))
        create_backup(self.src, self.backup_dir, now_utc=_NOW)
        self.assertIsNone(backup_reminder(self.src, now_utc=_NOW))  # just backed up
        self.assertIn("days ago", str(backup_reminder(self.src, now_utc="2026-12-01T00:00:00+00:00")))

    def test_the_reminder_threshold_is_thirty_days(self) -> None:
        """Bounded by how long a good copy survives to repair from, not by how often
        disks fail: the mirror quarantines a replaced copy for `mirror_retention_days`
        (30), and past that the faulty file may be the only version left. The same
        figure as `scrub.REMIND_AFTER_DAYS`, so there is one number to remember."""
        record_backup(self.src, "2026-06-01T00:00:00+00:00")
        self.assertIsNone(
            backup_reminder(self.src, now_utc="2026-06-30T00:00:00+00:00"), "29 days is not overdue"
        )
        self.assertIsNotNone(
            backup_reminder(self.src, now_utc="2026-07-01T00:00:00+00:00"), "30 days is overdue"
        )

    def test_restore_missing_archive_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            restore_from_archive(self.src, self.tmp / "nope.tar.gz", now_utc=_NOW)


class TestEncryptedBackup(_BackupEnv):
    def test_encrypted_archive_is_not_a_plain_tar_but_roundtrips(self) -> None:
        report = create_backup(self.src, self.backup_dir, now_utc=_NOW, passphrase="s3cret")
        archive = Path(report.archive)
        self.assertTrue(report.encrypted)
        self.assertTrue(archive.name.endswith(".tar.gz.enc"))
        self.assertTrue(is_encrypted_archive(archive))
        with self.assertRaises(tarfile.ReadError):  # it's encrypted, not a tar
            tarfile.open(archive, "r:*")

        # roundtrip into a fresh location with the right passphrase
        _set_env(self.tmp / "dst")
        dst = default_runtime_paths()
        ensure_runtime_layout(dst)
        restored = restore_from_archive(dst, archive, now_utc=_NOW, passphrase="s3cret")
        self.assertEqual(restored.documents_restored, 2)
        self.assertEqual((dst.library_root / "Personal/a.txt").read_bytes(), b"alpha")

    def test_wrong_passphrase_fails(self) -> None:
        report = create_backup(self.src, self.backup_dir, now_utc=_NOW, passphrase="right")
        _set_env(self.tmp / "dst")
        dst = default_runtime_paths()
        ensure_runtime_layout(dst)
        with self.assertRaises(ValueError):
            restore_from_archive(dst, Path(report.archive), now_utc=_NOW, passphrase="wrong")

    def test_encrypted_archive_needs_a_passphrase(self) -> None:
        report = create_backup(self.src, self.backup_dir, now_utc=_NOW, passphrase="x")
        with self.assertRaises(ValueError):
            restore_from_archive(self.src, Path(report.archive), now_utc=_NOW)  # no passphrase


if __name__ == "__main__":
    unittest.main()
