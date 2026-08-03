# pyright: reportUnknownVariableType=false
"""A preserve-zone document must survive the next rescan.

The bug these exist for. Everything indexed in place — an archived document, a
music album, a repository's working tree — was catalogued by one rescan and
declared DELETED by the next, with its stored path wiped, while the file sat
untouched on disk. Search then lost it silently.

The cause was a mismatch of scope: `reconcile` was given the files this pass
MANAGES (`walk_library_files`, which deliberately excludes preserve zones) but ALL
the catalog rows, and decided deletion by "is this row's path in that list". A row
is deleted when its FILE is gone, not when this pass happens not to manage it.

Nothing caught it because every existing test ran a single rescan. The bug only
appears on the second, which is why several of these run it three times: once to
index, once to expose the bug, once to prove the fix is not merely delaying it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import run_rescan
from procrafiler.rescan import DELETED_STATUS, reconcile

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


class ReconcileScopeTests(unittest.TestCase):
    """The pure function, where the mistake actually lived."""

    @staticmethod
    def _row(path: Path, digest: str = "abc") -> dict[str, object]:
        return {"current_path": str(path), "sha256": digest, "status": "LIBRARY_STORED"}

    def test_a_preserved_file_is_not_a_deletion(self) -> None:
        kept = Path("/library/Personal/Archive/note.txt")
        plan = reconcile([], [self._row(kept)], lambda _p: "abc", [kept])
        self.assertEqual(plan.deleted, [], "an existing file was reported as deleted")

    def test_a_genuinely_missing_file_is_still_a_deletion(self) -> None:
        """Anti-vacuity: the fix must not simply switch deletion detection off."""
        gone = Path("/library/Personal/Misc/gone.txt")
        plan = reconcile([], [self._row(gone)], lambda _p: "abc", [])
        self.assertEqual(len(plan.deleted), 1)

    def test_a_preserved_file_is_never_moved_or_re_ingested(self) -> None:
        """It counts as PRESENT, not as managed. Anything else would rename an
        album's tracks or re-date an archived document."""
        preserved = Path("/library/Media/Music/Album/01.mp3")
        plan = reconcile([], [], lambda _p: "abc", [preserved])
        self.assertEqual(plan.new_files, [])
        self.assertEqual(plan.moved, [])
        self.assertEqual(plan.duplicates, [])

    def test_omitting_the_preserved_list_keeps_the_old_behaviour(self) -> None:
        """The parameter is optional; callers that manage everything they list are
        unaffected."""
        gone = Path("/library/Personal/Misc/gone.txt")
        plan = reconcile([], [self._row(gone)], lambda _p: "abc")
        self.assertEqual(len(plan.deleted), 1)

    def test_a_managed_copy_of_a_preserved_file_is_a_duplicate_not_a_move(self) -> None:
        """The preserved original still exists, so a copy appearing elsewhere is a
        second thing, not the same one having moved."""
        original = Path("/library/Personal/Archive/note.txt")
        copy = Path("/library/Personal/Misc/note.txt")
        plan = reconcile([copy], [self._row(original)], lambda _p: "abc", [original])
        self.assertEqual(plan.deleted, [])
        self.assertEqual([str(p) for p, _row in plan.duplicates], [str(copy)])


class SurvivesRepeatedRescanTests(unittest.TestCase):
    """End to end, through the real rescan, run more than once."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        self.tmp.cleanup()

    _REPLY = json.dumps({
        "name": "Note", "category_path": "Personal/Misc", "date": "2026-04-30",
        "series": False, "summary": "s", "keywords": ["edf"], "entities": {}, "language": "fr",
    })

    def _rescan(self, times: int = 1) -> None:
        for _ in range(times):
            with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY):
                run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

    def _rows(self) -> list[dict[str, object]]:
        return CatalogRepository(self.paths.catalog_db_file).list_documents()

    def _live(self) -> list[dict[str, object]]:
        return [r for r in self._rows() if r.get("status") != DELETED_STATUS]

    def test_an_archived_document_is_still_in_the_catalog_after_three_rescans(self) -> None:
        archive = self.paths.library_root / "Personal" / "Archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "old-note.txt").write_text("Facture EDF du 30 avril 2026", encoding="utf-8")

        self._rescan(3)
        live = self._live()
        self.assertEqual(len(live), 1, f"the archived document was lost: {self._rows()}")
        self.assertEqual(live[0]["current_filename"], "old-note.txt")

    def test_its_stored_path_is_not_wiped(self) -> None:
        """The row was not merely flagged — its path was emptied, so even a repair
        had nothing left to point at."""
        archive = self.paths.library_root / "Work" / "Archive"
        archive.mkdir(parents=True, exist_ok=True)
        note = archive / "contract.txt"
        note.write_text("Contrat de prestation, 12 mars 2026", encoding="utf-8")

        self._rescan(2)
        self.assertEqual(str(self._live()[0]["current_path"]), str(note))

    def test_the_file_itself_is_never_touched(self) -> None:
        archive = self.paths.library_root / "Personal" / "Archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "old-note.txt").write_text("Facture EDF", encoding="utf-8")

        self._rescan(2)
        self.assertEqual([p.name for p in archive.iterdir()], ["old-note.txt"])

    def test_a_document_deleted_by_hand_is_still_detected(self) -> None:
        """Anti-vacuity, end to end: deletion detection must still work."""
        archive = self.paths.library_root / "Personal" / "Archive"
        archive.mkdir(parents=True, exist_ok=True)
        note = archive / "old-note.txt"
        note.write_text("Facture EDF", encoding="utf-8")

        self._rescan()
        self.assertEqual(len(self._live()), 1)
        note.unlink()
        self._rescan()
        self.assertEqual(self._live(), [], "a file removed by hand stayed live in the catalog")

    def test_a_normal_document_still_round_trips(self) -> None:
        """The managed path is untouched by the fix."""
        folder = self.paths.library_root / "Personal" / "Misc"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "note.txt").write_text("Facture EDF du 30 avril 2026", encoding="utf-8")

        self._rescan(3)
        self.assertEqual(len(self._live()), 1)

    @unittest.skipUnless(_HAS_FFMPEG, "ffmpeg not installed")
    def test_a_music_album_is_still_in_the_catalog_after_three_rescans(self) -> None:
        """The media zone shipped with this bug: an album was catalogued once and
        gone by the next rescan."""
        album = self.paths.library_root / "Media" / "Music" / "Kind of Blue"
        album.mkdir(parents=True, exist_ok=True)
        for n in (1, 2, 3):
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=1", str(album / f"0{n} Track {n}.mp3")],
                check=True, capture_output=True,
            )

        self._rescan(3)
        self.assertEqual(len(self._live()), 3, f"the album was lost: {self._rows()}")

    def test_re_indexing_is_never_paid_for_twice(self) -> None:
        """The other half of the same mistake: rows wiped of their path stop
        matching `known_paths`, so a later rescan would re-analyse — and re-bill —
        everything it had already read."""
        archive = self.paths.library_root / "Personal" / "Archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "old-note.txt").write_text("Facture EDF du 30 avril 2026", encoding="utf-8")

        self._rescan()
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY) as again:
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
        again.assert_not_called()


if __name__ == "__main__":
    unittest.main()
