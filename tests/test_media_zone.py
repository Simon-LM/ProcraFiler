# pyright: reportUnknownVariableType=false
"""The media zone — where files are catalogued without ever being read.

Everywhere else ProcraFiler opens the file. Here it must not, and that single
promise is what most of these tests exist to defend: no audio transcribed, no
frame extracted, no image looked at, not one byte of media leaving the machine.

The AI is not excluded from this zone, it is moved: it reads the metadata written
into the file, the filename, and above all the folder name — which for an album or
a series is usually the most informative thing there is. So the tests come in two
halves: what we DO read (and that we speak each metadata format correctly), and
what we must never read, however convenient it would be.
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

from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.media_metadata import (
    MediaDescription,
    describe_media,
    format_media_description,
)
from procrafiler.taxonomy import (
    BASE_LIBRARY_DIRECTORIES,
    category_label,
    classifiable_categories,
    ensure_base_library_directories,
    is_in_media_zone,
)

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _tagged_audio(path: Path, **tags: str) -> Path:
    """A real one-second MP3 carrying real tags, written by ffmpeg."""
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    for key, value in tags.items():
        command += ["-metadata", f"{key}={value}"]
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True)
    return path


class TaxonomyTests(unittest.TestCase):
    def test_media_sits_at_the_top_level(self) -> None:
        """Personal and Work say what a document is ABOUT; Media says how a file is
        TREATED. A processing rule buried under a subject branch becomes invisible —
        and an album is neither personal nor professional."""
        self.assertIn(("Media",), BASE_LIBRARY_DIRECTORIES)
        self.assertIn(("Media", "Music"), BASE_LIBRARY_DIRECTORIES)
        self.assertIn(("Media", "Films"), BASE_LIBRARY_DIRECTORIES)

    def test_the_ai_can_never_file_anything_into_it(self) -> None:
        """The strongest guarantee of the zone. Its files are deliberately never
        read, so the model has no basis to send anything there — and a run must not
        be able to move a document in by mistake."""
        labels = [category_label(c) for c in classifiable_categories()]
        for label in labels:
            with self.subTest(label=label):
                self.assertFalse(label.startswith("Media"), f"{label} is offered to the AI")

    def test_membership_is_by_prefix_not_by_name(self) -> None:
        self.assertTrue(is_in_media_zone(("Media",)))
        self.assertTrue(is_in_media_zone(("Media", "Music", "Kind of Blue")))
        self.assertFalse(is_in_media_zone(("Personal", "Media")))
        self.assertFalse(is_in_media_zone(("Media-Kit",)))
        self.assertFalse(is_in_media_zone(()))

    def test_the_folders_are_scaffolded_so_the_user_can_find_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_base_library_directories(root)
            self.assertTrue((root / "Media" / "Music").is_dir())
            self.assertTrue((root / "Media" / "Films").is_dir())


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg/ffprobe not installed")
class MetadataReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_written_tags_are_read(self) -> None:
        track = _tagged_audio(
            self.dir / "01 So What.mp3",
            album="Kind of Blue", artist="Miles Davis", title="So What",
            track="1", date="1959", genre="Jazz",
        )
        tags = describe_media(track).tags
        self.assertEqual(tags["album"], "Kind of Blue")
        self.assertEqual(tags["artist"], "Miles Davis")
        self.assertEqual(tags["title"], "So What")
        self.assertEqual(tags["genre"], "Jazz")

    def test_the_technical_shape_is_read(self) -> None:
        """Not decoration: it separates a two-hour film from a four-minute track
        when the names alone do not."""
        described = describe_media(_tagged_audio(self.dir / "x.mp3"))
        self.assertIn("duration", described.technical)
        self.assertIn("audio", described.technical)

    def test_a_file_with_no_tags_at_all_still_describes_itself(self) -> None:
        """The normal case for a WAV, and for anything stripped on export. The name
        and the folder are still there, and they are still enough to catalog it."""
        album = self.dir / "Kind of Blue"
        album.mkdir()
        described = describe_media(_tagged_audio(album / "01 So What.mp3"))
        self.assertFalse(described.has_tags)
        self.assertEqual(described.filename, "01 So What.mp3")
        self.assertEqual(described.folder, "Kind of Blue")

    def test_the_folder_name_is_captured_relative_to_the_library(self) -> None:
        album = self.dir / "Media" / "Music" / "Miles Davis" / "Kind of Blue"
        album.mkdir(parents=True)
        described = describe_media(_tagged_audio(album / "01.mp3"), self.dir)
        self.assertEqual(described.folder, "Kind of Blue")
        self.assertEqual(described.parent_folder, "Miles Davis")

    def test_a_very_long_tag_is_truncated(self) -> None:
        """An embedded booklet or lyric sheet would swamp the prompt without saying
        any more about what the file IS."""
        described = describe_media(_tagged_audio(self.dir / "x.mp3", comment="z" * 5000))
        self.assertLessEqual(len(described.tags.get("comment", "")), 400)


class ImageMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_an_images_caption_is_read_and_its_pixels_are_not(self) -> None:
        from PIL import Image

        photo = self.dir / "cover.jpg"
        image = Image.new("RGB", (8, 8), color="blue")
        exif = image.getexif()
        exif[270] = "Album cover, 1959"  # ImageDescription
        exif[315] = "Jim Marshall"       # Artist
        image.save(photo, exif=exif)

        tags = describe_media(photo).tags
        self.assertEqual(tags["description"], "Album cover, 1959")
        self.assertEqual(tags["artist"], "Jim Marshall")

    def test_an_image_without_metadata_is_not_an_error(self) -> None:
        from PIL import Image

        photo = self.dir / "plain.png"
        Image.new("RGB", (8, 8)).save(photo)
        self.assertFalse(describe_media(photo).has_tags)


class NeverRaisesTests(unittest.TestCase):
    """A media library holds odd files. None of them may stop a rescan."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_corrupt_file(self) -> None:
        broken = self.dir / "broken.mp3"
        broken.write_bytes(b"\x00\x01 not audio")
        described = describe_media(broken)
        self.assertEqual(described.filename, "broken.mp3")

    def test_a_missing_file(self) -> None:
        self.assertEqual(describe_media(self.dir / "gone.mp3").filename, "gone.mp3")

    def test_a_path_outside_the_library_root(self) -> None:
        """Never a crash: the folder name is simply taken from the path itself."""
        described = describe_media(self.dir / "x.mp3", Path("/somewhere/else"))
        self.assertEqual(described.filename, "x.mp3")

    def test_ffmpeg_absent_leaves_names_working(self) -> None:
        with patch("procrafiler.media_metadata.ffmpeg_available", return_value=False):
            described = describe_media(self.dir / "Album" / "01.mp3")
        self.assertEqual(described.filename, "01.mp3")
        self.assertFalse(described.has_tags)


class PromptTests(unittest.TestCase):
    """What the model is told — and, more importantly, what it is told NOT to do."""

    def test_it_states_outright_that_nothing_was_opened(self) -> None:
        """Without this, a model handed 'Kind of Blue, Miles Davis, Jazz' will
        happily summarise music it never heard, and that invention flows into the
        catalog and the search index as though it were a reading."""
        text = format_media_description(MediaDescription(filename="01.mp3", folder="Kind of Blue"))
        lowered = text.lower()
        self.assertIn("not opened", lowered)
        self.assertIn("never describe sound or pictures you have not been given", lowered)

    def test_the_folder_name_is_flagged_as_the_best_clue(self) -> None:
        text = format_media_description(MediaDescription(filename="01.mp3", folder="Kind of Blue"))
        self.assertIn("Kind of Blue", text)
        self.assertIn("most reliable identification", text)

    def test_absent_metadata_is_declared_rather_than_left_blank(self) -> None:
        """"No tags" and "I forgot to look" must not read the same."""
        text = format_media_description(MediaDescription(filename="01.wav", folder="Session"))
        self.assertIn("NONE", text)

    def test_every_tag_present_reaches_the_text(self) -> None:
        text = format_media_description(
            MediaDescription(filename="01.mp3", tags={"album": "Kind of Blue", "artist": "Miles Davis"})
        )
        self.assertIn("Kind of Blue", text)
        self.assertIn("Miles Davis", text)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg/ffprobe not installed")
class RescanTests(unittest.TestCase):
    """The zone end to end, through the real rescan."""

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
        self.album = self.paths.library_root / "Media" / "Music" / "Kind of Blue"
        self.album.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        self.tmp.cleanup()

    def _album_of(self, count: int) -> list[Path]:
        return [
            _tagged_audio(
                self.album / f"{n:02d} Track {n}.mp3",
                album="Kind of Blue", artist="Miles Davis", title=f"Track {n}", track=str(n),
            )
            for n in range(1, count + 1)
        ]

    @staticmethod
    def _reply() -> str:
        return json.dumps({
            "name": "Kind of Blue", "date": "1959-08-17", "category_path": "Media/Music",
            "series": False, "summary": "Album de jazz de Miles Davis.",
            "keywords": ["jazz", "Miles Davis", "album"], "entities": {"artist": "Miles Davis"},
            "language": "fr",
        })

    def test_one_album_costs_one_ai_call_not_one_per_track(self) -> None:
        """An album is a set. Twelve tracks share one album name, one artist and one
        year; asking twelve times buys twelve near-identical answers at twelve times
        the price."""
        from procrafiler.pipeline import run_rescan

        self._album_of(12)
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()) as call:
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
        self.assertEqual(call.call_count, 1, "one call per FOLDER, not per file")

    def test_the_call_receives_metadata_and_never_content(self) -> None:
        """The promise of the whole zone, asserted on the actual prompt."""
        from procrafiler.pipeline import run_rescan

        self._album_of(3)
        seen: list[str] = []

        def capture(prompt: str, *args: object, **kwargs: object) -> str:
            seen.append(prompt)
            return self._reply()

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=capture):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        self.assertTrue(seen)
        prompt = seen[0]
        self.assertIn("Kind of Blue", prompt)
        self.assertIn("Miles Davis", prompt)
        self.assertIn("NOT opened", prompt)
        # A LATER track, deliberately: the first one is already named by the
        # single-file description above, so asserting on it would pass even with no
        # track list at all. `01 … 02 … 03` is what says "album" — no single name does.
        self.assertIn("03 Track 3.mp3", prompt, "the track list is part of the evidence")

    def test_an_untagged_album_is_still_recognisable_from_its_track_list(self) -> None:
        """The case the track list exists for, and the common one: a folder of WAVs
        with no metadata whatsoever. Nothing else in the prompt then carries the
        sequence, and `01 … 02 … 03` is what says "album" — no single name does.
        """
        from procrafiler.pipeline import run_rescan

        for n in range(1, 4):
            _tagged_audio(self.album / f"{n:02d} Movement {n}.wav")

        seen: list[str] = []

        def capture(prompt: str, *args: object, **kwargs: object) -> str:
            seen.append(prompt)
            return self._reply()

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=capture):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        self.assertTrue(seen)
        for track in ("01 Movement 1.wav", "02 Movement 2.wav", "03 Movement 3.wav"):
            with self.subTest(track=track):
                self.assertIn(track, seen[0])

    def test_no_audio_is_ever_transcribed_and_no_frame_ever_extracted(self) -> None:
        """The expensive mistake this zone exists to prevent. Transcribing an album
        buys lyrics at best, and describing frames of a film costs a great deal to
        learn what the title already said."""
        from procrafiler.pipeline import run_rescan

        self._album_of(4)
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()) as analysis, \
             patch("procrafiler.av_reader.read_audio_video") as av, \
             patch("procrafiler.ai_transcribe.transcribe") as transcribe, \
             patch("procrafiler.media_tools.extract_frames") as frames, \
             patch("procrafiler.ai_reader.read_visual") as visual:
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        # Anti-vacuity: without this, indexing nothing at all would pass every
        # assertion below.
        analysis.assert_called_once()
        av.assert_not_called()
        transcribe.assert_not_called()
        frames.assert_not_called()
        visual.assert_not_called()

    def test_the_files_are_neither_renamed_nor_moved(self) -> None:
        """An album's track order IS the album; a timestamp prefix would break it
        and every player that reads it."""
        from procrafiler.pipeline import run_rescan

        before = sorted(p.name for p in self._album_of(5))
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
        self.assertEqual(sorted(p.name for p in self.album.iterdir()), before)

    def test_every_track_is_catalogued_and_shares_the_albums_fiche(self) -> None:
        from procrafiler.catalog import CatalogRepository
        from procrafiler.pipeline import run_rescan

        self._album_of(6)
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        rows = CatalogRepository(self.paths.catalog_db_file).list_documents()
        fiches = [json.loads(r["content_json"]) for r in rows]
        self.assertEqual(len(fiches), 6)
        for fiche in fiches:
            with self.subTest(name=fiche["name"]):
                self.assertIn("jazz", fiche["keywords"])
                self.assertEqual(fiche["read_via"], "metadata")
                self.assertTrue(fiche["media_zone"])

    def test_the_fiche_takes_the_users_name_even_when_the_ai_proposes_one(self) -> None:
        """Asserted on the indexing step itself, not through the whole rescan: the
        later name-sync phase would mask a regression here by overwriting the fiche
        name from the on-disk stem anyway. The zone must be right on its own."""
        from procrafiler.catalog import CatalogRepository
        from procrafiler.pipeline import _index_media_folder

        files = self._album_of(2)
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()):
            _index_media_folder(
                self.paths, self.album, files,
                now_utc=None, features={}, emit=lambda _m: None,
            )

        names = {
            json.loads(r["content_json"])["name"]
            for r in CatalogRepository(self.paths.catalog_db_file).list_documents()
        }
        self.assertEqual(names, {"01 Track 1", "02 Track 2"}, "the AI's album name overwrote the track names")

    def test_the_catalogued_name_is_the_users_own(self) -> None:
        """Whoever made this album named its tracks. Our naming convention is for
        documents."""
        from procrafiler.catalog import CatalogRepository
        from procrafiler.pipeline import run_rescan

        self._album_of(2)
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        names = {json.loads(r["content_json"])["name"] for r in CatalogRepository(self.paths.catalog_db_file).list_documents()}
        self.assertEqual(names, {"01 Track 1", "02 Track 2"})

    def test_two_albums_are_two_calls(self) -> None:
        """The folder is the unit. Two albums must not be merged into one fiche."""
        from procrafiler.pipeline import run_rescan

        self._album_of(3)
        other = self.paths.library_root / "Media" / "Music" / "Blue Train"
        other.mkdir(parents=True)
        _tagged_audio(other / "01 Blue Train.mp3", album="Blue Train", artist="John Coltrane")

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()) as call:
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
        self.assertEqual(call.call_count, 2)

    def test_a_second_rescan_costs_nothing(self) -> None:
        """Already-catalogued files are not re-analysed — otherwise every rescan of
        a music library would be a bill."""
        from procrafiler.pipeline import run_rescan

        self._album_of(4)
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._reply()) as again:
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
        again.assert_not_called()

    def test_a_documents_archive_is_still_read_in_full(self) -> None:
        """The neighbouring zone must not be caught by this change: an Archive
        folder holds DOCUMENTS, and being able to search inside them is the entire
        reason it exists."""
        from procrafiler.pipeline import run_rescan

        archive = self.paths.library_root / "Personal" / "Archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "old-note.txt").write_text("Facture EDF du 30 avril 2026", encoding="utf-8")

        seen: list[str] = []

        def capture(prompt: str, *args: object, **kwargs: object) -> str:
            seen.append(prompt)
            return self._reply()

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=capture):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        self.assertTrue(any("Facture EDF" in p for p in seen), "the archived document was not read")


if __name__ == "__main__":
    unittest.main()
