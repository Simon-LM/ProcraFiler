# pyright: reportUnknownVariableType=false
"""Not paying to transcribe silence, and paying less for the rest.

Two things that both act on the transcription bill, and one shared trap.

**The probe.** Two audio files in a real run were transcribed end to end — ten
minutes of music, 62% of that run's bill — to discover there was nothing to
transcribe. Listening to a few seconds first buys the same answer for a fraction.

**The speed-up.** Transcription is billed per second SUBMITTED, so sending the
audio at 1.25x is 20% off every recording, and 1.5x is 33% off.

**The trap they share** is timestamps. A sped-up file reports a passage at 100 s
that really happened at 125 s, and nothing downstream knows: the frame planner
takes the number at face value and hands it to ffmpeg. Forgetting the conversion
raises nothing, fails nothing, and logs nothing — it silently cuts every still
from the wrong moment, drifting further the later the passage. That is why it is a
named function with its own tests, and why several of them are here.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from procrafiler import av_reader, media_tools
from procrafiler.ai_transcribe import (
    TranscriptResult,
    TranscriptSegment,
    rescale_to_source_time,
)
from procrafiler.av_reader import (
    MIN_PROBE_DURATION,
    PROBE_SOURCE_SPAN,
    PROBE_WINDOWS,
    PROBE_WINDOW_SECONDS,
    _probe_offsets,
    has_any_speech,
    read_audio_video,
)
from procrafiler.media_tools import (
    DEFAULT_TRANSCRIBE_SPEED,
    MAX_TRANSCRIBE_SPEED,
    MediaProbe,
    _audio_filters,
    transcribe_speed,
)


def _speaks() -> TranscriptResult:
    return TranscriptResult(
        text="bonjour", audio_seconds=8,
        segments=[TranscriptSegment(start=1.0, end=3.0, text="bonjour")],
    )


def _silent() -> TranscriptResult:
    return TranscriptResult(text="", audio_seconds=8)


class SpeedSettingTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_TRANSCRIBE_SPEED", None)

    def test_the_default_is_the_cautious_one(self) -> None:
        """1.25 rather than 1.5: the margin that survives a studio interview does
        not survive a phone memo in a noisy room, and we cannot tell which we have."""
        self.assertEqual(transcribe_speed(), DEFAULT_TRANSCRIBE_SPEED)
        self.assertLess(DEFAULT_TRANSCRIBE_SPEED, MAX_TRANSCRIBE_SPEED)

    def test_a_reckless_value_is_clamped_not_obeyed(self) -> None:
        """4x would give a cheap bill and an unusable transcript — the worst of
        both, and paid for."""
        os.environ["PROCRAFILER_TRANSCRIBE_SPEED"] = "4"
        self.assertEqual(transcribe_speed(), MAX_TRANSCRIBE_SPEED)

    def test_slowing_down_is_refused(self) -> None:
        """Below 1.0 the file gets LONGER, so it costs more. Never by accident."""
        os.environ["PROCRAFILER_TRANSCRIBE_SPEED"] = "0.5"
        self.assertEqual(transcribe_speed(), 1.0)

    def test_it_can_be_switched_off(self) -> None:
        os.environ["PROCRAFILER_TRANSCRIBE_SPEED"] = "1.0"
        self.assertEqual(transcribe_speed(), 1.0)

    def test_nonsense_falls_back_to_the_default(self) -> None:
        os.environ["PROCRAFILER_TRANSCRIBE_SPEED"] = "fast"
        self.assertEqual(transcribe_speed(), DEFAULT_TRANSCRIBE_SPEED)

    def test_the_filter_is_only_added_when_it_does_something(self) -> None:
        self.assertEqual(_audio_filters(1.0), [])
        self.assertIn("atempo=1.25", _audio_filters(1.25))


class TimestampConversionTests(unittest.TestCase):
    """The trap. Every assertion here is about a silent, invisible failure."""

    def test_timestamps_come_back_on_the_recordings_own_clock(self) -> None:
        sped_up = TranscriptResult(
            text="x", segments=[TranscriptSegment(start=100.0, end=104.0, text="x")]
        )
        segment = rescale_to_source_time(sped_up, 1.25).segments[0]
        self.assertAlmostEqual(segment.start, 125.0)
        self.assertAlmostEqual(segment.end, 130.0)

    def test_the_drift_grows_with_the_passage(self) -> None:
        """Why a single spot-check is not enough: an error here is small at the
        start of a film and minutes wide at the end."""
        result = TranscriptResult(
            text="x",
            segments=[
                TranscriptSegment(start=10.0, end=12.0, text="a"),
                TranscriptSegment(start=1600.0, end=1610.0, text="b"),
            ],
        )
        rescaled = rescale_to_source_time(result, 1.5)
        self.assertAlmostEqual(rescaled.segments[0].start, 15.0)
        self.assertAlmostEqual(rescaled.segments[1].start, 2400.0)

    def test_no_speed_up_means_no_change(self) -> None:
        result = TranscriptResult(text="x", segments=[TranscriptSegment(1.0, 2.0, "x")])
        self.assertIs(rescale_to_source_time(result, 1.0), result)

    def test_the_billed_seconds_are_not_rescaled(self) -> None:
        """`audio_seconds` is what the provider CHARGED, and it charged for the
        shortened audio it received. Scaling it up would inflate every usage report
        and quietly ruin the calibration built from past runs."""
        result = TranscriptResult(
            text="x", audio_seconds=80, segments=[TranscriptSegment(1.0, 2.0, "x")]
        )
        self.assertEqual(rescale_to_source_time(result, 1.25).audio_seconds, 80)

    def test_the_text_and_the_reason_survive(self) -> None:
        result = TranscriptResult(
            text="bonjour", reason=None, provider="mistral", model="voxtral-mini-latest",
            segments=[TranscriptSegment(1.0, 2.0, "bonjour")],
        )
        rescaled = rescale_to_source_time(result, 1.5)
        self.assertEqual(rescaled.text, "bonjour")
        self.assertEqual(rescaled.model, "voxtral-mini-latest")

    def test_a_transcript_with_no_segments_is_untouched(self) -> None:
        result = TranscriptResult(text="", audio_seconds=3)
        self.assertIs(rescale_to_source_time(result, 1.25), result)


class ProbeWindowTests(unittest.TestCase):
    def test_it_listens_in_three_places_not_just_the_opening(self) -> None:
        """A conference opens on music, a film on its titles, a phone recording on
        fumbling. Judging any of those from their first ten seconds would throw the
        recording away."""
        offsets = _probe_offsets(1800.0)
        self.assertEqual(len(offsets), PROBE_WINDOWS)
        self.assertEqual(offsets, sorted(offsets))
        self.assertLess(offsets[0], 1800 * 0.2)
        self.assertGreater(offsets[-1], 1800 * 0.7)

    def test_no_window_runs_past_the_end(self) -> None:
        """Measured against the span a window actually COVERS, not the span it
        submits: at 1.5x, ten seconds of upload is fifteen seconds of recording, and
        reserving only ten would push the last window off the end."""
        self.assertGreater(PROBE_SOURCE_SPAN, PROBE_WINDOW_SECONDS)
        for duration in (121.0, 300.0, 3600.0):
            with self.subTest(duration=duration):
                for offset in _probe_offsets(duration):
                    self.assertLessEqual(offset + PROBE_SOURCE_SPAN, duration + 0.01)

    def test_the_first_window_is_not_at_zero(self) -> None:
        """Second zero is routinely a fade or a black frame."""
        self.assertGreater(_probe_offsets(600.0)[0], 0.0)


class ProbeBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _extractor(_src, dst, **_kwargs):  # noqa: ANN001, ANN205
        dst.write_bytes(b"audio")
        return True

    def test_speech_in_the_first_window_stops_the_probe_there(self) -> None:
        """The common case must be the cheap one: most spoken recordings say
        something in their first seconds, and paying for three windows to confirm
        what one already showed would eat the saving."""
        with patch.object(av_reader, "extract_audio", self._extractor), \
             patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _speaks()) as call:
            found, _billed = has_any_speech(Path("x.mp3"), 1800.0, self.work)
        self.assertTrue(found)
        self.assertEqual(call.call_count, 1)

    def test_silence_everywhere_costs_three_windows_and_answers_no(self) -> None:
        with patch.object(av_reader, "extract_audio", self._extractor), \
             patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _silent()) as call:
            found, _billed = has_any_speech(Path("x.mp3"), 1800.0, self.work)
        self.assertFalse(found)
        self.assertEqual(call.call_count, PROBE_WINDOWS)

    def test_speech_late_in_the_recording_is_still_found(self) -> None:
        """A film that opens on five minutes of score."""
        replies = [_silent(), _silent(), _speaks()]
        with patch.object(av_reader, "extract_audio", self._extractor), \
             patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: replies.pop(0)):
            found, _billed = has_any_speech(Path("x.mp3"), 1800.0, self.work)
        self.assertTrue(found)

    def test_a_failed_extraction_assumes_there_IS_speech(self) -> None:
        """The probe exists to save money, never to lose a recording. Anything it
        cannot answer must fall through to the real transcription."""
        with patch.object(av_reader, "extract_audio", lambda *a, **k: False), \
             patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _silent()):
            found, _billed = has_any_speech(Path("x.mp3"), 1800.0, self.work)
        self.assertTrue(found)

    def test_a_provider_error_assumes_there_IS_speech(self) -> None:
        error = TranscriptResult(reason="transcription_failed:503")
        with patch.object(av_reader, "extract_audio", self._extractor), \
             patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: error):
            found, _billed = has_any_speech(Path("x.mp3"), 1800.0, self.work)
        self.assertTrue(found)

    def test_what_the_probe_cost_is_reported(self) -> None:
        """It is billed audio like any other, and a run that hides it under-reports
        what it spent."""
        with patch.object(av_reader, "extract_audio", self._extractor), \
             patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _silent()):
            _found, billed = has_any_speech(Path("x.mp3"), 1800.0, self.work)
        self.assertEqual(billed, 8 * PROBE_WINDOWS)


class ReaderIntegrationTests(unittest.TestCase):
    """The two features where they actually meet the reader."""

    def tearDown(self) -> None:
        av_reader.extract_audio = media_tools.extract_audio
        av_reader.probe_media = media_tools.probe_media
        os.environ.pop("PROCRAFILER_TRANSCRIBE_SPEED", None)

    def _audio_of(self, seconds: float) -> None:
        av_reader.probe_media = lambda _p: MediaProbe(
            duration_seconds=seconds, has_audio=True, has_video=False, ok=True
        )

    def test_music_is_never_transcribed_in_full(self) -> None:
        """The measured failure this exists for: ten minutes of music transcribed
        end to end for an empty fiche."""
        self._audio_of(600.0)
        calls: list[float | None] = []

        def _extract(_src, dst, *, max_seconds=None, speed=1.0, start_seconds=None):  # noqa: ANN001
            calls.append(start_seconds)
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _extract
        with patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _silent()):
            result = read_audio_video(Path("music.mp3"))

        self.assertTrue(all(offset is not None for offset in calls),
                        "the whole file was extracted for transcription")
        self.assertTrue(any("no recognisable speech" in note for note in result.notes))

    def test_a_spoken_recording_is_transcribed_in_full_after_the_probe(self) -> None:
        self._audio_of(600.0)
        full: list[bool] = []

        def _extract(_src, dst, *, max_seconds=None, speed=1.0, start_seconds=None):  # noqa: ANN001
            full.append(start_seconds is None)
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _extract
        with patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _speaks()):
            read_audio_video(Path("talk.mp3"))
        self.assertTrue(any(full), "the probe found speech but the full pass never ran")

    def test_a_short_clip_is_not_probed_at_all(self) -> None:
        """Below a couple of minutes the probe costs about as much as the whole
        transcription — nothing to save, one more round trip to lose."""
        self._audio_of(MIN_PROBE_DURATION - 1)
        offsets: list[float | None] = []

        def _extract(_src, dst, *, max_seconds=None, speed=1.0, start_seconds=None):  # noqa: ANN001
            offsets.append(start_seconds)
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _extract
        with patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _speaks()):
            read_audio_video(Path("short.mp3"))
        self.assertEqual(offsets, [None], "a short clip was probed")

    def test_the_full_pass_is_sent_sped_up(self) -> None:
        self._audio_of(600.0)
        speeds: list[float] = []

        def _extract(_src, dst, *, max_seconds=None, speed=1.0, start_seconds=None):  # noqa: ANN001
            if start_seconds is None:
                speeds.append(speed)
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _extract
        with patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _speaks()):
            read_audio_video(Path("talk.mp3"))
        self.assertEqual(speeds, [DEFAULT_TRANSCRIBE_SPEED])

    def test_the_reader_returns_source_time_timestamps(self) -> None:
        """End to end, on the one thing that would fail silently: what the reader
        hands the frame planner must be the recording's own clock."""
        self._audio_of(600.0)
        os.environ["PROCRAFILER_TRANSCRIBE_SPEED"] = "1.5"

        def _extract(_src, dst, **_kwargs):  # noqa: ANN001
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _extract
        with patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _speaks()):
            result = read_audio_video(Path("talk.mp3"))

        assert result.transcript is not None
        self.assertAlmostEqual(result.transcript.segments[0].start, 1.5, places=3)

    def test_the_probes_seconds_are_added_to_the_bill(self) -> None:
        self._audio_of(600.0)

        def _extract(_src, dst, **_kwargs):  # noqa: ANN001
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _extract
        with patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _speaks()):
            result = read_audio_video(Path("talk.mp3"))

        assert result.transcript is not None
        # 8 s of probe (one window, speech found immediately) + 8 s of full pass.
        self.assertEqual(result.transcript.audio_seconds, 16)

    def test_a_silent_result_still_reports_what_the_probe_cost(self) -> None:
        """Money was spent even though nothing was transcribed; a run that showed
        zero here would under-report its own bill."""
        self._audio_of(600.0)

        def _extract(_src, dst, **_kwargs):  # noqa: ANN001
            dst.write_bytes(b"audio")
            return True

        av_reader.extract_audio = _extract
        with patch.object(av_reader, "transcribe", side_effect=lambda *a, **k: _silent()):
            result = read_audio_video(Path("music.mp3"))

        assert result.transcript is not None
        self.assertEqual(result.transcript.audio_seconds, 8 * PROBE_WINDOWS)


class ForecastTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"] = "mistral:voxtral-mini-latest"

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_TRANSCRIBE_SPEED", None)
        os.environ.pop("PROCRAFILER_AI_TRANSCRIBE_PRIMARY", None)

    def test_the_forecast_prices_what_will_be_sent_not_what_was_recorded(self) -> None:
        """Forecasting the raw duration would over-state every recording by the
        speed factor — and the spend ceiling checks the upper bound, so an inflated
        estimate stops runs that were never going to cost that much."""
        from procrafiler.ai_estimate import estimate_ai_calls

        with tempfile.TemporaryDirectory() as tmp:
            talk = Path(tmp) / "talk.mp3"
            talk.write_bytes(b"x")
            work_sets = [("", [talk])]
            with patch(
                "procrafiler.ai_estimate.probe_media",
                return_value=MediaProbe(duration_seconds=600.0, has_audio=True, has_video=False, ok=True),
            ):
                os.environ["PROCRAFILER_TRANSCRIBE_SPEED"] = "1.0"
                plain = estimate_ai_calls(work_sets)
                os.environ["PROCRAFILER_TRANSCRIBE_SPEED"] = "1.25"
                sped = estimate_ai_calls(work_sets)

        self.assertEqual(plain.audio_seconds, 600)
        self.assertEqual(sped.audio_seconds, 480)


if __name__ == "__main__":
    unittest.main()
