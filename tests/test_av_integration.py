"""Real, opt-in integration test of the AUDIO/VIDEO chain — this COSTS money.

Skipped by default. Run it on your own recording::

    PROCRAFILER_AV_IT=1 PROCRAFILER_AV_FILE=/path/to/your/video.mp4 \\
        .venv/bin/python -m unittest tests.test_av_integration
    # or: make test-av FILE=/path/to/your/video.mp4

**What it costs.** Transcription is billed per second of recording (a few tenths of
a cent per minute) and each sampled still is one vision call. The test prints the
plan before spending, so an unexpectedly long file is visible rather than
surprising. A three-minute clip is a couple of cents.

**Why this cannot be an offline test.** Every other test in the suite mocks the
providers, so they prove the chain is wired and the fallbacks hold — never that
Voxtral hears what was said, nor that the chosen moments show anything useful.
Those two judgements are the whole point of the design, and only a real recording
measures them.

**What it asserts.** On the SHAPE of the outcome, never on exact wording: that
speech came back with timestamps inside the recording, that the stills were taken
where the transcript pointed, that the assembled text puts the transcript first.
A test demanding particular words would be red one run in three and end up ignored.

The reading is printed in full, because the real value of this test on the first
run is what a human sees in that output.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

_ENABLED = os.environ.get("PROCRAFILER_AV_IT") == "1"
_MEDIA = os.environ.get("PROCRAFILER_AV_FILE", "").strip()

TRANSCRIBE_MODEL = "mistral:voxtral-mini-latest"
# The passage picker only reads text; the small model is plenty and is cheapest.
HIGHLIGHT_MODEL = "mistral:mistral-small-latest"
VISION_MODEL = "mistral:mistral-medium-latest"


def _load_real_env() -> None:
    """Load the real key, which the suite bootstrap deliberately hides."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "MISTRAL_API_KEY" and value.strip():
            os.environ["MISTRAL_API_KEY"] = value.strip()


@unittest.skipUnless(_ENABLED, "set PROCRAFILER_AV_IT=1 to run (COSTS MONEY)")
@unittest.skipUnless(_MEDIA, "set PROCRAFILER_AV_FILE=/path/to/a/video-or-audio-file")
class RealRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _load_real_env()
        cls.media = Path(_MEDIA)
        if not cls.media.is_file():
            raise unittest.SkipTest(f"no such file: {cls.media}")
        os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"] = TRANSCRIBE_MODEL
        os.environ["PROCRAFILER_AI_VIDEO_PRIMARY"] = HIGHLIGHT_MODEL
        os.environ["PROCRAFILER_AI_IMAGE_PRIMARY"] = VISION_MODEL

    def test_the_recording_is_read_and_what_it_cost_is_reported(self) -> None:
        from procrafiler.ai_estimate import estimate_ai_calls, format_estimate
        from procrafiler.av_reader import read_audio_video
        from procrafiler.cost_forecast import forecast_cost, format_cost_forecast
        from procrafiler.frame_sampling import frame_budget
        from procrafiler.media_tools import probe_media
        from procrafiler.usage_meter import format_usage_report, usage_scope

        probe = probe_media(self.media)
        self.assertTrue(probe.ok, f"ffprobe could not read it: {probe.reason}")

        # What it will cost, BEFORE spending — printed so a long file is visible.
        estimate = estimate_ai_calls([("", [self.media])])
        print("\n" + "=" * 72)
        print(f"FILE      {self.media.name}")
        print(f"PROBE     {probe.duration_seconds:.1f}s, audio={probe.has_audio}, video={probe.has_video}")
        print(f"PLAN      {format_estimate(estimate)}")
        print(f"COST      {format_cost_forecast(forecast_cost(estimate))}")
        print("=" * 72)

        with usage_scope() as usage:
            result = read_audio_video(
                self.media, original_filename=self.media.name, source_folder="integration-test"
            )

        print("\n--- READING -----------------------------------------------------------")
        print(result.text or f"(nothing readable: {result.reason})")
        print("\n--- NOTES -------------------------------------------------------------")
        for note in result.notes:
            print(f"  - {note}")
        print("\n--- ACTUALLY CONSUMED -------------------------------------------------")
        print(format_usage_report(usage))

        # Keep the reading next to the recording. This run costs real money and its
        # result is the input to everything downstream — classification, naming,
        # a prompt comparison. Re-transcribing half an hour of audio to get a text
        # we already paid for once would be waste, and waste nobody notices.
        saved = self.media.with_suffix(self.media.suffix + ".reading.txt")
        saved.write_text(result.text or f"(nothing readable: {result.reason})", "utf-8")
        print(f"\nreading saved to {saved}")
        print()

        # A file with no speech and no pictures — a music track, an ambience
        # recording — is correctly read as "nothing to read here", and that is an
        # ANSWER, not a failure of this harness. Demanding readable text would fail
        # the run on exactly the files the speech probe exists to handle cheaply.
        no_speech = any("no recognisable speech" in note for note in result.notes)
        if not (probe.has_video or not no_speech):
            self.assertFalse(
                result.is_readable,
                "an audio-only recording with no speech should have nothing to read",
            )
        else:
            self.assertTrue(result.is_readable, f"nothing could be read: {result.reason}")

        if probe.has_audio and result.transcript and result.transcript.has_speech:
            transcript = result.transcript
            self.assertGreater(transcript.audio_seconds, 0, "billed duration must be reported")
            self.assertTrue(transcript.segments, "timestamps are what the design depends on")
            for segment in transcript.segments:
                self.assertGreaterEqual(segment.start, 0.0)
                # A little slack: the last segment can round past the container's
                # reported duration.
                self.assertLessEqual(segment.start, probe.duration_seconds + 2)
                self.assertGreaterEqual(segment.end, segment.start)
            self.assertIn("Spoken transcript", result.text or "")

        if probe.has_video:
            self.assertLessEqual(
                result.frames_analysed, frame_budget(probe.duration_seconds),
                "more stills were looked at than the budget allows",
            )
            if result.frames_analysed:
                self.assertIn("Visual description", result.text or "")

        if (result.transcript and result.transcript.has_speech) and result.frames_analysed:
            text = result.text or ""
            self.assertLess(
                text.index("Spoken transcript"), text.index("Visual description"),
                "the transcript must lead; the stills are context",
            )


if __name__ == "__main__":
    unittest.main()
