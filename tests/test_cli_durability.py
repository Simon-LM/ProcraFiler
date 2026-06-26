"""P3 — CLI dispatch & exit codes for the durability commands.

The durability subsystem (scrub / verify-catalog / backup / restore) has thorough
module-level tests; what was untested is the **CLI layer**: that `main([...])`
parses the flags and returns the right **exit code** (scrubbed scripts and cron
rely on `scrub`/`verify-catalog` exiting non-zero on a problem, and on `restore`
failing cleanly on a bad path). These tests drive the real commands end-to-end
through `main()`, offline, no AI.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from uuid import uuid4

from procrafiler.catalog import CatalogRepository
from procrafiler.cli import main
from procrafiler.config import default_runtime_paths, ensure_runtime_layout


class _CliEnv(unittest.TestCase):
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

    def _seed(self, rel: str, content: bytes, *, mirror: bool = True) -> Path:
        """A filed document on disk (+ optional mirror copy) with a matching
        catalog row — the realistic precondition the durability commands expect."""
        lib = self.paths.library_root / rel
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_bytes(content)
        if mirror:
            mir = self.paths.mirror_root / rel
            mir.parent.mkdir(parents=True, exist_ok=True)
            mir.write_bytes(content)
        self.catalog.upsert_document(
            doc_id=str(uuid4()), sha256=hashlib.sha256(content).hexdigest(),
            current_filename=lib.name, current_path=str(lib),
            status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00+00:00",
        )
        return lib

    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()


class TestScrubCli(_CliEnv):
    def test_clean_library_returns_0(self) -> None:
        self._seed("Personal/a.txt", b"hello")
        code, _ = self._run(["scrub"])
        self.assertEqual(code, 0)

    def test_corruption_returns_1(self) -> None:
        lib = self._seed("Personal/a.txt", b"original", mirror=False)
        lib.write_bytes(b"tampered")  # hash now differs from the catalog
        code, out = self._run(["scrub", "--no-mirror"])
        self.assertEqual(code, 1)
        self.assertIn("a.txt", out)

    def test_repair_heals_from_mirror_returns_0(self) -> None:
        lib = self._seed("Personal/a.txt", b"original")  # good mirror copy exists
        lib.write_bytes(b"tampered")
        code, _ = self._run(["scrub", "--repair"])
        self.assertEqual(code, 0)
        self.assertEqual(lib.read_bytes(), b"original")  # healed from the mirror


class TestVerifyCatalogCli(_CliEnv):
    def test_healthy_catalog_returns_0(self) -> None:
        self._seed("Personal/a.txt", b"hello")
        code, _ = self._run(["verify-catalog"])
        self.assertEqual(code, 0)


class TestBackupRestoreCli(_CliEnv):
    def _backup_dir(self) -> Path:
        d = self.tmp_path / "backups"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_backup_writes_dated_archive_returns_0(self) -> None:
        self._seed("Personal/a.txt", b"hello")
        dest = self._backup_dir()
        code, _ = self._run(["backup", "--to", str(dest)])
        self.assertEqual(code, 0)
        self.assertEqual(len(list(dest.glob("*.tar.gz"))), 1)
        self.assertEqual(len(list(dest.glob("*.sha256"))), 1)

    def test_backup_encrypt_returns_0(self) -> None:
        self._seed("Personal/a.txt", b"hello")
        dest = self._backup_dir()
        os.environ["PROCRAFILER_BACKUP_PASSPHRASE"] = "correct horse"
        code, _ = self._run(["backup", "--to", str(dest), "--encrypt"])
        self.assertEqual(code, 0)
        self.assertEqual(len(list(dest.glob("*.enc"))), 1)
        self.assertEqual(list(dest.glob("*.tar.gz")), [])  # plaintext bundle not left behind

    def test_restore_from_archive_roundtrip_returns_0(self) -> None:
        lib = self._seed("Personal/a.txt", b"hello")
        dest = self._backup_dir()
        self.assertEqual(self._run(["backup", "--to", str(dest)])[0], 0)
        lib.unlink()  # simulate a loss of the filed document
        archive = next(dest.glob("*.tar.gz"))
        code, _ = self._run(["restore", "--from-archive", str(archive)])
        self.assertEqual(code, 0)
        self.assertTrue(lib.is_file())  # document restored to the library
        self.assertEqual(lib.read_bytes(), b"hello")

    def test_restore_missing_archive_returns_1(self) -> None:
        code, _ = self._run(["restore", "--from-archive", str(self.tmp_path / "nope.tar.gz")])
        self.assertEqual(code, 1)


class TestDeletedHistoryCli(_CliEnv):
    def test_no_deletions_returns_0(self) -> None:
        code, out = self._run(["deleted-history", "--limit", "10"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())  # prints a friendly "nothing yet" line, not empty


if __name__ == "__main__":
    unittest.main()
