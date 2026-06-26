"""P2 — mirror correctness & consistency (mocked AI, no GPU, offline).

The mirror is meant to be a FAITHFUL path-for-path replica of the library — the
foundation of the durability model. These tests pin two invariants that were
otherwise only covered indirectly:

  * after a real ``process-all``, every filed document exists in the mirror at the
    same relative path with identical bytes (and nothing extra) — and turning the
    mirror feature off writes nothing;
  * a hand move/rename of a library file drags its mirror copy (and the mirror's
    text sidecar) along, instead of orphaning the old copy with nothing at the new
    path (the gap this batch fixes).

Trash / hand-deletion / sidecar-backup mirror behaviour is already covered by
test_library_trash, test_rescan and test_sidecars.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.catalog import CatalogRepository
from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    set_feature_flag,
)
from procrafiler.pipeline import (
    _file_sha256,
    _sidecar_path,
    process_all_inbox_files,
    run_rescan,
)


def _doc_files(root: Path) -> set[Path]:
    """Real document files under a tree, relative to it — skipping hidden dot
    entries (``.procrafiler`` metadata, hidden text sidecars) on BOTH sides so the
    comparison is about the visible documents."""
    return {
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
    }


def _set_env(root: Path) -> None:
    os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "Inbox")
    os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "Library")
    os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "Mirror")
    os.environ["PROCRAFILER_HOME"] = str(root / ".state")
    os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")


_ENV_KEYS = (
    "PROCRAFILER_WORKSPACE_DIR", "PROCRAFILER_LIBRARY_DIR", "PROCRAFILER_LIBRARY_MIRROR_DIR",
    "PROCRAFILER_HOME", "PROCRAFILER_CONFIG_HOME", "PROCRAFILER_AI_ANALYSIS_PRIMARY",
)


class TestProcessAllMirrorsFaithfully(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        _set_env(Path(self.tmp.name))
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp.cleanup()

    @staticmethod
    def _fiche(name: str, category: str) -> str:
        return (
            '{"name": "%s", "category_path": "%s", "summary": "s", "keywords": ["k"]}'
            % (name, category)
        )

    def test_every_filed_document_is_mirrored_byte_for_byte(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"Releve de compte BNP avril 2026")
        (self.paths.inbox_dir / "b.txt").write_bytes(b"Facture EDF mai 2026")
        fiches = iter([
            self._fiche("Releve BNP", "Personal/Administrative/Banking"),
            self._fiche("Facture EDF", "Personal/Administrative/Utilities"),
        ])
        last = self._fiche("Facture EDF", "Personal/Administrative/Utilities")

        def _fake(*_a: object, **_k: object) -> str:
            return next(fiches, last)

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=_fake):
            process_all_inbox_files(self.paths, now_utc=self.now)

        lib = _doc_files(self.paths.library_root)
        mir = _doc_files(self.paths.mirror_root)
        self.assertGreaterEqual(len(lib), 2)          # both documents were filed
        self.assertEqual(lib, mir)                     # faithful replica: same paths, no extras
        for rel in lib:                                # …and identical bytes
            self.assertEqual(
                (self.paths.library_root / rel).read_bytes(),
                (self.paths.mirror_root / rel).read_bytes(),
            )

    def test_mirror_disabled_writes_no_mirror_files(self) -> None:
        set_feature_flag(self.paths, "mirror_sync", False)
        (self.paths.inbox_dir / "a.txt").write_bytes(b"Releve de compte BNP avril 2026")
        with patch(
            "procrafiler.ai_analysis.call_mistral_chat",
            return_value=self._fiche("Releve BNP", "Personal/Administrative/Banking"),
        ):
            process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertGreaterEqual(len(_doc_files(self.paths.library_root)), 1)  # filed
        self.assertEqual(_doc_files(self.paths.mirror_root), set())           # nothing mirrored


class TestMirrorFollowsHandMove(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        _set_env(Path(self.tmp.name))
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 6, 17, 9, 0, 0, tzinfo=timezone.utc)
        self.repo = CatalogRepository(self.paths.catalog_db_file)
        self.repo.init_schema()

    def tearDown(self) -> None:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _emit(self, _m: str) -> None:
        pass

    def test_mirror_copy_and_sidecar_follow_the_move(self) -> None:
        lib = self.paths.library_root
        rel_old = Path("Personal") / "2026-04-30_00-00-00__Releve.txt"
        rel_new = Path("Personal") / "Administrative" / "Banking" / "2026-04-30_00-00-00__Releve.txt"
        # The user already moved the document in the library (it's on disk at the
        # new path). The catalog still points at the old path.
        new = lib / rel_new
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(b"releve content")
        # The mirror still holds the copy + its text sidecar at the OLD path.
        mirror_old = self.paths.mirror_root / rel_old
        mirror_old.parent.mkdir(parents=True, exist_ok=True)
        mirror_old.write_bytes(b"releve content")
        mirror_sidecar_old = _sidecar_path(mirror_old)
        mirror_sidecar_old.write_text("ocr text", encoding="utf-8")
        self.repo.upsert_document(
            doc_id="doc-1", sha256=_file_sha256(new), current_filename=rel_new.name,
            current_path=str(lib / rel_old), status="LIBRARY_STORED",
            updated_at_utc="2026-01-01T00:00:00Z",
        )

        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["moved"], 1)

        mirror_new = self.paths.mirror_root / rel_new
        # The mirror copy followed to the new path; the old orphan is gone.
        self.assertTrue(mirror_new.is_file())
        self.assertEqual(mirror_new.read_bytes(), b"releve content")
        self.assertFalse(mirror_old.exists())
        # The mirror's text sidecar followed too.
        self.assertTrue(_sidecar_path(mirror_new).is_file())
        self.assertFalse(mirror_sidecar_old.exists())

    def test_missing_mirror_copy_is_tolerated(self) -> None:
        # Mirror off / out of sync: no copy at the old path → the move still
        # succeeds and nothing is invented in the mirror (no crash).
        lib = self.paths.library_root
        rel_new = Path("Personal") / "Administrative" / "Banking" / "2026-04-30_00-00-00__Releve.txt"
        new = lib / rel_new
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(b"releve content")
        self.repo.upsert_document(
            doc_id="doc-1", sha256=_file_sha256(new), current_filename=rel_new.name,
            current_path=str(lib / "Personal" / "2026-04-30_00-00-00__Releve.txt"),
            status="LIBRARY_STORED", updated_at_utc="2026-01-01T00:00:00Z",
        )
        counts = run_rescan(self.paths, now_utc=self.now, features={}, emit=self._emit)
        self.assertEqual(counts["moved"], 1)
        self.assertEqual(_doc_files(self.paths.mirror_root), set())


if __name__ == "__main__":
    unittest.main()
