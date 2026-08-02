# pyright: reportUnknownVariableType=false
"""Reading a video by listening to it first, and looking only where that told us to.

Mistral has no video model, and going through speech turns out to be both cheaper
and better: transcription is billed per second of recording, vision per image, and
the transcript is the part that carries the meaning. So the chain is probe →
transcribe → choose moments → cut a few stills → look at those.

Every link can be absent. A user may not have ffmpeg; a video may be silent; the
audio may be music; a provider may fail. **None of those is an error** — each has a
defined fallback, and this file exists mostly to pin those fallbacks, because they
are what stands between "one odd file" and a crashed run over someone's archive.

The empty-transcript case is not hypothetical: it was verified against the live
API, where a pure tone returns HTTP 200 with `text: ""` and `segments: []`.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from procrafiler import av_reader, media_tools
from procrafiler.ai_transcribe import TranscriptResult, TranscriptSegment, format_transcript
from procrafiler.av_reader import read_audio_video
from procrafiler.media_tools import MediaProbe

FFMPEG = media_tools.ffmpeg_available()


def _silent_video(path: Path, seconds: int = 4) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"testsrc=size=320x240:rate=10:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def _tone_audio(path: Path, seconds: int = 3) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}", "-ar", "16000", "-ac", "1", str(path)],
        check=True, capture_output=True,
    )


class _Patched(unittest.TestCase):
    """Swap the AI calls out; none of these tests may reach a network."""

    def setUp(self) -> None:
        self._saved = {
            name: getattr(av_reader, name)
            for name in ("transcribe", "select_highlights", "read_visual")
        }

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(av_reader, name, value)

    def _no_ai(self) -> None:
        av_reader.transcribe = lambda *a, **k: TranscriptResult(reason="chain_not_configured")
        av_reader.select_highlights = lambda *a, **k: []
        av_reader.read_visual = lambda *a, **k: _Visual(None)

    @staticmethod
    def _speaks(text: str = "This is a water damage report for the kitchen."):
        return TranscriptResult(
            text=text,
            segments=[TranscriptSegment(start=1.0, end=3.0, text=text)],
            audio_seconds=8,
            provider="mistral",
            model="voxtral-mini-latest",
        )


class _Visual:
    """Stands in for `ai_reader.VisualRead` — what one frame's reading looks like."""

    def __init__(self, text: str | None, read_via: str = "vision") -> None:
        self.text = text
        self.read_via = read_via

    @property
    def is_readable(self) -> bool:
        return bool(self.text and self.text.strip())

    @property
    def used_ocr(self) -> bool:
        return self.read_via == "ocr"


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealMediaTests(_Patched):
    """Against genuine files produced by ffmpeg — the probe is not mocked, so the
    container parsing is exercised for real."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def test_a_silent_video_is_still_read_from_its_frames(self) -> None:
        """No audio track at all: transcription is skipped entirely — not attempted
        and failed — and the stills carry the read."""
        video = self.dir / "clip.mp4"
        _silent_video(video)
        attempted: list[int] = []
        av_reader.transcribe = lambda *a, **k: attempted.append(1) or TranscriptResult()
        av_reader.select_highlights = lambda *a, **k: []
        av_reader.read_visual = lambda *a, **k: _Visual("a colour test pattern")

        result = read_audio_video(video)

        self.assertEqual(attempted, [], "nothing to transcribe, so nothing was paid for")
        self.assertTrue(result.is_readable)
        self.assertGreater(result.frames_analysed, 0)
        self.assertIn("no audio track", " ".join(result.notes))

    def test_an_audio_only_file_takes_no_frames(self) -> None:
        audio = self.dir / "note.mp3"
        _tone_audio(audio)
        looked: list[int] = []
        av_reader.transcribe = lambda *a, **k: self._speaks()
        av_reader.select_highlights = lambda *a, **k: []
        av_reader.read_visual = lambda *a, **k: looked.append(1) or _Visual("x")

        result = read_audio_video(audio)

        self.assertEqual(looked, [], "an audio file has nothing to look at")
        self.assertEqual(result.frames_analysed, 0)
        self.assertTrue(result.is_readable)
        self.assertIn("water damage", result.text or "")

    def test_music_without_speech_is_a_result_not_a_failure(self) -> None:
        """Verified live: a tone returns 200 with an empty transcript. Treating
        that as an error would send every home video to manual review."""
        video = self.dir / "music.mp4"
        _silent_video(video)
        av_reader.transcribe = lambda *a, **k: TranscriptResult(audio_seconds=4)  # empty text
        av_reader.select_highlights = lambda *a, **k: []
        av_reader.read_visual = lambda *a, **k: _Visual("a colour test pattern")

        result = read_audio_video(video)

        self.assertTrue(result.is_readable, "the stills still described it")
        self.assertNotIn("[Spoken transcript", result.text or "")

    def test_a_corrupt_file_is_reported_not_crashed(self) -> None:
        broken = self.dir / "broken.mp4"
        broken.write_bytes(b"this is not a video at all")
        result = read_audio_video(broken)
        self.assertFalse(result.is_readable)
        self.assertIsNotNone(result.reason)

    def test_frames_are_capped_however_long_the_video(self) -> None:
        video = self.dir / "clip.mp4"
        _silent_video(video)
        self._no_ai()
        seen: list[int] = []
        av_reader.read_visual = lambda *a, **k: seen.append(1) or _Visual("x")
        read_audio_video(video)
        self.assertLessEqual(len(seen), av_reader.MAX_FRAMES_HARD_CAP)


class DegradationTests(_Patched):
    """The probe is stubbed here, so a machine without ffmpeg still runs them."""

    def setUp(self) -> None:
        super().setUp()
        self._probe = av_reader.probe_media

    def tearDown(self) -> None:
        av_reader.probe_media = self._probe
        super().tearDown()

    def test_no_ffmpeg_means_manual_review_not_a_crash(self) -> None:
        av_reader.probe_media = lambda _p: MediaProbe(reason="ffmpeg_not_installed")
        result = read_audio_video(Path("/nonexistent/clip.mp4"))
        self.assertFalse(result.is_readable)
        self.assertEqual(result.reason, "ffmpeg_not_installed")

    def test_a_zero_length_recording_is_refused(self) -> None:
        """Duration is what the whole cost is computed from; guessing one would be
        inventing the bill."""
        av_reader.probe_media = lambda _p: MediaProbe(duration_seconds=0.0, has_video=True, ok=True)
        self.assertEqual(read_audio_video(Path("x.mp4")).reason, "zero_duration")

    def test_nothing_readable_reaches_the_user_rather_than_being_guessed(self) -> None:
        """No speech AND no usable frame: the file must not be filed on the strength
        of its name."""
        av_reader.probe_media = lambda _p: MediaProbe(
            duration_seconds=30.0, has_audio=True, has_video=True, ok=True
        )
        av_reader.extract_audio = lambda *a, **k: False
        av_reader.extract_frames = lambda *a, **k: []
        try:
            result = read_audio_video(Path("x.mp4"))
        finally:
            av_reader.extract_audio = media_tools.extract_audio
            av_reader.extract_frames = media_tools.extract_frames
        self.assertFalse(result.is_readable)
        self.assertEqual(result.reason, "nothing_readable")

    def test_a_long_recording_is_truncated_and_says_so(self) -> None:
        """Transcription is billed by the second, so a forgotten four-hour recording
        must not silently become the most expensive file ever dropped."""
        av_reader.probe_media = lambda _p: MediaProbe(
            duration_seconds=4 * 3600, has_audio=True, has_video=False, ok=True
        )
        captured: dict[str, int | None] = {}

        def _fake_extract(_src, dst, *, max_seconds=None):  # noqa: ANN001
            captured["max_seconds"] = max_seconds
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _fake_extract
        av_reader.transcribe = lambda *a, **k: self._speaks()
        try:
            result = read_audio_video(Path("x.mp3"))
        finally:
            av_reader.extract_audio = media_tools.extract_audio

        self.assertEqual(captured["max_seconds"], av_reader.max_transcribe_seconds())
        self.assertTrue(
            any("transcribed" in note for note in result.notes),
            f"the truncation must be declared, got {result.notes}",
        )

    def test_a_two_hour_recording_cannot_exceed_the_hard_frame_cap(self) -> None:
        """The budget table already caps, but this is the second lock: a mistake in
        that table must not be able to turn one dropped file into a large bill. The
        real-media test above uses a short clip, where the cap never binds — so
        only a long duration exercises it."""
        av_reader.probe_media = lambda _p: MediaProbe(
            duration_seconds=2 * 3600, has_audio=False, has_video=True, ok=True
        )
        requested: list[float] = []

        def _record(_src, timestamps, _out):  # noqa: ANN001
            requested.extend(timestamps)
            return []

        av_reader.extract_frames = _record
        self._no_ai()
        try:
            read_audio_video(Path("long.mp4"))
        finally:
            av_reader.extract_frames = media_tools.extract_frames

        self.assertGreater(len(requested), 0)
        self.assertLessEqual(len(requested), av_reader.MAX_FRAMES_HARD_CAP)

    def test_the_transcript_leads_and_the_stills_follow(self) -> None:
        """Order and labels carry the weighting — the same device already used for
        a photographed document. Speech is deliberate; a still is an inference."""
        av_reader.probe_media = lambda _p: MediaProbe(
            duration_seconds=60.0, has_audio=True, has_video=True, ok=True
        )
        av_reader.extract_audio = lambda _s, dst, **k: (dst.write_bytes(b"a"), True)[1]
        av_reader.extract_frames = lambda _s, ts, out: [Path("f.jpg")]
        av_reader.transcribe = lambda *a, **k: self._speaks()
        av_reader.select_highlights = lambda *a, **k: []
        av_reader.read_visual = lambda *a, **k: _Visual("a wet ceiling")
        try:
            text = read_audio_video(Path("x.mp4")).text or ""
        finally:
            av_reader.extract_audio = media_tools.extract_audio
            av_reader.extract_frames = media_tools.extract_frames

        self.assertLess(
            text.index("Spoken transcript"), text.index("Visual description"),
            "the transcript must come first",
        )
        self.assertIn("less reliable", text)


class FramesGetTheSameReadingAsAPhotographTests(_Patched):
    """A document filmed in a video must be transcribed, not described — exactly
    like the same document photographed.

    This behaviour shipped in #116 for photos, but it lived INLINE in the pipeline,
    so this reader could not reach it and silently did without. Extracting it into
    `ai_reader.read_visual` is what closes that gap; these tests are what stop it
    reopening.
    """

    def setUp(self) -> None:
        super().setUp()
        self._probe = av_reader.probe_media
        av_reader.probe_media = lambda _p: MediaProbe(
            duration_seconds=60.0, has_audio=False, has_video=True, ok=True
        )
        av_reader.extract_frames = lambda _s, ts, out: [Path(f"f{i}.jpg") for i in range(len(ts))]

    def tearDown(self) -> None:
        av_reader.probe_media = self._probe
        av_reader.extract_frames = media_tools.extract_frames
        super().tearDown()

    def test_a_filmed_document_is_transcribed_not_described(self) -> None:
        from procrafiler.ai_reader import VisualRead

        av_reader.read_visual = lambda *a, **k: VisualRead(
            text="[Transcription OCR — fiable]\nFACTURE N° 4417 — 128,40 EUR",
            read_via="ocr",
        )
        try:
            result = read_audio_video(Path("x.mp4"))
        finally:
            av_reader.read_visual = self._saved["read_visual"]

        self.assertIn("FACTURE N° 4417", result.text or "", "the transcription must reach the analysis")
        self.assertGreater(result.transcribed_frames, 0, "the OCR re-read must be counted")

    def test_an_ordinary_scene_costs_no_ocr_call(self) -> None:
        """A photo of water damage — or a logo — triggers none. The confirmation is
        for dense written documents, and paying for it on every frame would double
        the cost of every video."""
        from procrafiler.ai_reader import VisualRead

        av_reader.read_visual = lambda *a, **k: VisualRead(text="a wet ceiling", read_via="vision")
        try:
            result = read_audio_video(Path("x.mp4"))
        finally:
            av_reader.read_visual = self._saved["read_visual"]

        self.assertGreater(result.frames_analysed, 0)
        self.assertEqual(result.transcribed_frames, 0)


class TranscriptFormattingTests(unittest.TestCase):
    def test_timestamps_are_kept_in_the_text(self) -> None:
        """They are what the highlight pass reasons over, and they let the analysis
        say WHEN something was said."""
        result = TranscriptResult(
            text="a b",
            segments=[
                TranscriptSegment(0.5, 2.0, "Hello."),
                TranscriptSegment(75.0, 78.0, "The leak is here."),
            ],
        )
        formatted = format_transcript(result)
        self.assertIn("[0:00] Hello.", formatted)
        self.assertIn("[1:15] The leak is here.", formatted)

    def test_a_long_transcript_is_truncated_visibly(self) -> None:
        segments = [TranscriptSegment(float(i), float(i + 1), "word " * 20) for i in range(500)]
        formatted = format_transcript(TranscriptResult(text="x", segments=segments), max_chars=500)
        self.assertLess(len(formatted), 700)
        self.assertIn("truncated", formatted)

    def test_hours_are_rendered_as_hours(self) -> None:
        result = TranscriptResult(text="x", segments=[TranscriptSegment(3725.0, 3730.0, "late")])
        self.assertIn("[1:02:05]", format_transcript(result))


if __name__ == "__main__":
    unittest.main()
