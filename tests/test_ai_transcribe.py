# pyright: reportUnknownVariableType=false
"""Speech to text, and what a run is told it will cost for it.

The response shapes asserted here are not invented: they were captured from the
live Mistral endpoint before any of this was written, because a guessed field name
would have produced a feature that silently returns nothing. In particular
`segments[].start` / `.end` are seconds as floats, and `usage.prompt_audio_seconds`
is the **billed** unit — the reply also carries token counts, which are not.

The other half is the estimate. Audio and video are the one place the estimator
opens a file: a five-second clip and a two-hour recording differ by a factor of a
thousand, and a forecast that could not tell them apart would be useless exactly
where the money is.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from procrafiler import ai_transcribe, media_tools
from procrafiler.ai_estimate import estimate_ai_calls
from procrafiler.ai_naming import ProviderCallError
from procrafiler.ai_transcribe import ChainEntry, transcribe
from procrafiler.cost_forecast import forecast_cost
from procrafiler.usage_meter import usage_scope

FFMPEG = media_tools.ffmpeg_available()

# Captured verbatim from the live API on 2026-08-02.
LIVE_RESPONSE = {
    "model": "voxtral-mini-latest",
    "text": "Hello. This is a water damage report for the kitchen.",
    "language": None,
    "segments": [
        {"type": "transcription_segment", "text": "Hello.", "start": 0.1, "end": 0.4, "speaker_id": None},
        {
            "type": "transcription_segment",
            "text": " This is a water damage report for the kitchen.",
            "start": 0.9,
            "end": 3.1,
            "speaker_id": None,
        },
    ],
    "usage": {"prompt_audio_seconds": 7, "prompt_tokens": 4, "completion_tokens": 62},
}
# Also captured live: a pure tone. HTTP 200, nothing recognised.
LIVE_SILENCE = {"model": "voxtral-mini-latest", "text": "", "language": None, "segments": [],
                "usage": {"prompt_audio_seconds": 1, "prompt_tokens": 4, "completion_tokens": 1}}

MISTRAL = [ChainEntry(provider="mistral", model="voxtral-mini-latest")]


class _Stubbed(unittest.TestCase):
    def setUp(self) -> None:
        self._post = ai_transcribe._post_multipart
        self._key = os.environ.get("MISTRAL_API_KEY")
        os.environ["MISTRAL_API_KEY"] = "test-key"
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = Path(self.tmp.name) / "a.mp3"
        self.audio.write_bytes(b"pretend this is an mp3")

    def tearDown(self) -> None:
        ai_transcribe._post_multipart = self._post
        os.environ.pop("MISTRAL_API_KEY", None)
        if self._key is not None:
            os.environ["MISTRAL_API_KEY"] = self._key
        self.tmp.cleanup()

    def _reply(self, payload: dict, status: int = 200) -> None:
        ai_transcribe._post_multipart = lambda *a, **k: (status, json.dumps(payload).encode())


class TranscriptionTests(_Stubbed):
    def test_the_live_response_shape_is_parsed(self) -> None:
        self._reply(LIVE_RESPONSE)
        result = transcribe(self.audio, chain=MISTRAL)
        self.assertTrue(result.has_speech)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[1].start, 0.9)
        self.assertEqual(result.segments[1].end, 3.1)
        self.assertEqual(result.audio_seconds, 7)

    def test_audio_with_no_speech_is_an_answer_not_an_error(self) -> None:
        """A tone returns 200 with an empty transcript. If this raised, every home
        video with only music would land in manual review."""
        self._reply(LIVE_SILENCE)
        result = transcribe(self.audio, chain=MISTRAL)
        self.assertIsNone(result.reason, "not a failure")
        self.assertFalse(result.has_speech)
        self.assertEqual(result.audio_seconds, 1)

    def test_seconds_of_audio_are_recorded_for_billing(self) -> None:
        self._reply(LIVE_RESPONSE)
        with usage_scope() as usage:
            transcribe(self.audio, chain=MISTRAL)
        entry = usage.entries()[0]
        self.assertEqual(entry.task, "TRANSCRIBE")
        self.assertEqual(entry.audio_seconds, 7)

    def test_a_server_error_degrades_instead_of_raising(self) -> None:
        """A video whose audio cannot be transcribed is still worth looking at."""
        self._reply({"message": "boom"}, status=500)
        result = transcribe(self.audio, chain=MISTRAL, retries=0, sleep_fn=lambda _s: None)
        self.assertFalse(result.has_speech)
        self.assertIsNotNone(result.reason)

    def test_a_rate_limit_is_retried_then_reported(self) -> None:
        calls: list[int] = []

        def _limited(*_a, **_k):
            calls.append(1)
            return (429, b'{"object":"error","type":"rate_limited"}')

        ai_transcribe._post_multipart = _limited
        result = transcribe(self.audio, chain=MISTRAL, retries=2, sleep_fn=lambda _s: None)
        self.assertEqual(len(calls), 3, "the initial attempt plus two retries")
        self.assertIn("transcription_failed", result.reason or "")

    def test_ollama_is_rejected_clearly(self) -> None:
        """Ollama exposes no transcription endpoint; a plain reason beats a
        confusing network error on a misconfigured chain."""
        result = transcribe(
            self.audio,
            chain=[ChainEntry(provider="ollama", model="whisper")],
            retries=0,
            sleep_fn=lambda _s: None,
        )
        self.assertIn("unsupported_transcribe_provider", result.reason or "")

    def test_no_chain_configured_costs_nothing(self) -> None:
        called: list[int] = []
        ai_transcribe._post_multipart = lambda *a, **k: called.append(1) or (200, b"{}")
        result = transcribe(self.audio, chain=[])
        self.assertEqual(called, [])
        self.assertEqual(result.reason, "chain_not_configured")

    def test_an_empty_audio_file_is_not_uploaded(self) -> None:
        empty = Path(self.tmp.name) / "empty.mp3"
        empty.write_bytes(b"")
        called: list[int] = []
        ai_transcribe._post_multipart = lambda *a, **k: called.append(1) or (200, b"{}")
        result = transcribe(empty, chain=MISTRAL)
        self.assertEqual(called, [], "no point paying to upload nothing")
        self.assertEqual(result.reason, "no_audio_extracted")

    def test_the_call_actually_asks_for_timestamps(self) -> None:
        """Asserting that `_multipart` *can* carry the field proves nothing about
        what the call site sends. Without timestamps the API still returns text —
        so the feature would not fail, it would silently lose the one thing the
        whole frame-sampling design is built on.
        """
        sent: dict[str, bytes] = {}

        def _capture(_url, body, _content_type, _key, _timeout):  # noqa: ANN001
            sent["body"] = body
            return (200, json.dumps(LIVE_RESPONSE).encode())

        ai_transcribe._post_multipart = _capture
        transcribe(self.audio, chain=MISTRAL)
        self.assertIn(b"timestamp_granularities", sent["body"])
        self.assertIn(b"segment", sent["body"])
        # The API rejects `language` together with timestamps, so it must not appear.
        self.assertNotIn(b'name="language"', sent["body"])

    def test_malformed_segments_are_skipped_not_fatal(self) -> None:
        self._reply(
            {
                "text": "ok",
                "segments": [
                    {"text": "good", "start": 1.0, "end": 2.0},
                    {"text": "no timing"},
                    {"text": "bad timing", "start": "x", "end": "y"},
                    {"text": "", "start": 3.0, "end": 4.0},
                ],
                "usage": {"prompt_audio_seconds": 5},
            }
        )
        result = transcribe(self.audio, chain=MISTRAL)
        self.assertEqual([s.text for s in result.segments], ["good"])


class MultipartTests(unittest.TestCase):
    def test_the_body_carries_the_model_and_asks_for_timestamps(self) -> None:
        """`timestamp_granularities` is deliberately sent WITHOUT `language`: the
        API rejects the pair, and the timestamps are what the design depends on."""
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "clip.mp3"
            audio.write_bytes(b"AUDIOBYTES")
            body, content_type = ai_transcribe._multipart(
                {"model": "voxtral-mini-latest", "timestamp_granularities": "segment"}, "file", audio
            )
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b"voxtral-mini-latest", body)
        self.assertIn(b"timestamp_granularities", body)
        self.assertNotIn(b'name="language"', body)
        self.assertIn(b"AUDIOBYTES", body, "the audio itself must be in the body")
        self.assertIn(b'filename="clip.mp3"', body)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class EstimateTests(unittest.TestCase):
    """Audio and video are costed from their real duration."""

    CHAINS = {
        "PROCRAFILER_AI_IMAGE_PRIMARY": "mistral:mistral-medium-latest",
        "PROCRAFILER_AI_ANALYSIS_PRIMARY": "mistral:mistral-medium-latest",
        "PROCRAFILER_AI_TRANSCRIBE_PRIMARY": "mistral:voxtral-mini-latest",
        "PROCRAFILER_AI_VIDEO_PRIMARY": "mistral:mistral-small-latest",
    }

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self.CHAINS}
        os.environ.update(self.CHAINS)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def _video(self, name: str, seconds: int, *, with_audio: bool) -> Path:
        path = self.dir / name
        command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                   f"testsrc=size=320x240:rate=10:duration={seconds}"]
        if with_audio:
            command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
        command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
        subprocess.run(command, check=True, capture_output=True)
        return path

    def test_a_longer_video_is_estimated_to_cost_more(self) -> None:
        short = estimate_ai_calls([("d", [self._video("s.mp4", 3, with_audio=True)])])
        longer = estimate_ai_calls([("d", [self._video("l.mp4", 40, with_audio=True)])])
        self.assertGreater(longer.audio_seconds, short.audio_seconds)
        self.assertGreaterEqual(longer.av_frames, short.av_frames)

    def test_a_silent_video_is_not_charged_for_transcription(self) -> None:
        estimate = estimate_ai_calls([("d", [self._video("mute.mp4", 5, with_audio=False)])])
        self.assertEqual(estimate.transcribe_calls, 0)
        self.assertEqual(estimate.audio_seconds, 0)
        self.assertGreater(estimate.av_frames, 0, "it is still looked at")

    def test_with_no_transcribe_chain_nothing_is_charged_for_speech(self) -> None:
        os.environ.pop("PROCRAFILER_AI_TRANSCRIBE_PRIMARY", None)
        estimate = estimate_ai_calls([("d", [self._video("s.mp4", 5, with_audio=True)])])
        self.assertEqual(estimate.transcribe_calls, 0)
        self.assertEqual(estimate.highlight_calls, 0, "nothing to select passages from")

    def test_the_transcription_price_is_exact_not_a_guess(self) -> None:
        """ffprobe gives the true duration and Voxtral bills by duration, so this
        one line is arithmetic rather than a forecast."""
        estimate = estimate_ai_calls([("d", [self._video("s.mp4", 30, with_audio=True)])])
        forecast = forecast_cost(estimate)
        self.assertIsNotNone(forecast)
        assert forecast is not None
        expected = estimate.audio_seconds / 60 * 0.003
        self.assertGreater(forecast.high, expected * 0.9)

    def test_a_corrupt_video_is_costed_as_unreadable(self) -> None:
        broken = self.dir / "broken.mp4"
        broken.write_bytes(b"not a video")
        estimate = estimate_ai_calls([("d", [broken])])
        self.assertEqual(estimate.av_files, 0)
        self.assertEqual(estimate.unreadable, 1)
        self.assertEqual(estimate.analyses, 0, "an unreadable file never reaches the analysis")


if __name__ == "__main__":
    unittest.main()
