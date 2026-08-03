"""Reading a video or an audio file — listen first, then look, and only where.

Mistral has no video model. That is not a gap to work around but the reason this
approach is cheaper *and* better than a video model would be:

1. **ffprobe** says how long it is and whether it has sound. Free, local.
2. **Voxtral transcribes the audio** with timestamps. Billed per second, so a
   whole hour costs less than a handful of images.
3. **A text pass reads the transcript** and names the moments worth seeing.
4. **ffmpeg cuts a few stills** at exactly those moments — plus the two ends.
5. **The vision model looks at those stills only**, and confirms the subject.

The transcript carries the *meaning*; the stills *confirm* it. Sampling a video
blind would cost several times more and still not know what was being said.

**Everything degrades, nothing fails.** Each stage can be absent or come back
empty, and every one of those is an ordinary outcome rather than an error:

| what happens | what we still do |
| --- | --- |
| ffmpeg not installed | manual review, no AI call, no crash |
| video has no audio track | skip transcription entirely, even spread of stills |
| audio is music or noise | transcript comes back empty — HTTP 200, not an error |
| transcription fails | stills on an even spread |
| highlight pass fails | stills on an even spread |
| some frames fail to extract | analyse the ones that worked |
| an audio-only file | transcript alone, no stills to take |
| nothing readable at all | manual review, and say why |

The last row is the one that matters most: a file we cannot read must reach the
user, never be filed on a guess.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from procrafiler.ai_highlights import select_highlights  # type: ignore[reportMissingImports]
from procrafiler.ai_reader import read_visual  # type: ignore[reportMissingImports]
from procrafiler.ai_transcribe import (  # type: ignore[reportMissingImports]
    TranscriptResult,
    format_transcript,
    rescale_to_source_time,
    transcribe,
)
from procrafiler.frame_sampling import (  # type: ignore[reportMissingImports]
    frame_budget,
    plan_frame_timestamps,
    select_distinct,
)
from procrafiler.media_tools import (  # type: ignore[reportMissingImports]
    DEFAULT_MAX_TRANSCRIBE_SECONDS,
    MediaProbe,
    extract_audio,
    extract_audio_windows,
    extract_frames,
    perceptual_hash,
    probe_media,
    transcribe_speed,
)

# Whatever the duration says it deserves, never send more stills than this in one
# read. The budget table already caps at 12; this is the second lock, so a bug in
# the table cannot turn one file into a large bill.
MAX_FRAMES_HARD_CAP = 12


@dataclass
class AVReadResult:
    text: str | None = None
    duration_seconds: float = 0.0
    transcript: TranscriptResult | None = None
    frames_analysed: int = 0
    # Frames that turned out to hold a written document and were re-read with OCR.
    transcribed_frames: int = 0
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_readable(self) -> bool:
        return bool(self.text and self.text.strip())


def max_transcribe_seconds() -> int:
    raw = os.environ.get("PROCRAFILER_MAX_TRANSCRIBE_SECONDS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TRANSCRIBE_SECONDS
    return value if value > 0 else DEFAULT_MAX_TRANSCRIBE_SECONDS


# The probe costs a fixed amount (PROBE_WINDOWS * PROBE_WINDOW_SECONDS of audio),
# so it is only worth making when a full transcription would cost distinctly more.
# At two minutes the full pass is about 96 s of billed audio against the probe's
# 40 s — still comfortably in favour of asking first. Below that the two converge
# and the probe becomes a round trip for nothing either way.
MIN_PROBE_DURATION = 2 * 60

# Five places, not one at the front. A conference opens on music, a film on its
# titles, a phone recording on fumbling — judging any of those from their first
# seconds would throw the recording away. Five rather than three because speech can
# be sparse: a documentary with long wordless stretches, an interview that starts
# late, a recording where someone speaks twice in half an hour. Every extra
# sampling point is another chance to catch it, and they cost the same in total.
PROBE_WINDOWS = 5
PROBE_WINDOW_SECONDS = 8

# The probe only has to answer "is anyone speaking", not what they said, so it can
# afford the fastest speed we allow.
PROBE_SPEED = 1.5

# ffmpeg's -t caps the OUTPUT, so a window submits PROBE_WINDOW_SECONDS and covers
# PROBE_WINDOW_SECONDS * PROBE_SPEED of the recording. The speed-up therefore buys
# more of the file heard for the same price rather than a cheaper probe — which is
# the better trade when the question is "is anyone speaking anywhere in here".
PROBE_SOURCE_SPAN = PROBE_WINDOW_SECONDS * PROBE_SPEED

# Where to listen, as fractions of the usable span. Spread across the whole
# recording and kept off both edges, which are routinely a fade or a black frame.
_PROBE_POSITIONS = (0.03, 0.26, 0.5, 0.74, 0.96)


def _probe_offsets(duration: float) -> list[float]:
    """Where to listen: spread across the recording, never at the very edges.

    The span each window covers is reserved at the end, so the last one lands
    inside the recording rather than running off it."""
    usable = max(0.0, duration - PROBE_SOURCE_SPAN)
    return [round(usable * fraction, 3) for fraction in _PROBE_POSITIONS][:PROBE_WINDOWS]


def has_any_speech(source: Path, duration: float, work: Path) -> tuple[bool, int]:
    """Listen to a few seconds in several places. Returns (speech found, seconds billed).

    Two audio files in a real run were transcribed end to end — ten minutes of
    music, 62% of that run's bill — to discover there was nothing to transcribe.
    This buys that answer for well under a minute of audio.

    The excerpts go up as **one file, in one request**, however many places are
    sampled. Asking five times in a row would sample the same recording no better
    and would multiply requests against a rate limit by five across a batch — so
    the cost is fixed and modest rather than variable and cheap, which is the right
    trade when the alternative is being throttled mid-run.

    Anything that goes wrong — extraction failure, a provider error — is treated as
    "there may well be speech": a probe must never be the reason a real recording
    goes unread.
    """
    sample = work / "probe.mp3"
    if not extract_audio_windows(
        source, sample, _probe_offsets(duration),
        window_seconds=PROBE_WINDOW_SECONDS, speed=PROBE_SPEED,
        work_dir=work / "probe_windows",
    ):
        return (True, 0)
    result = transcribe(sample)
    if result.reason:
        return (True, result.audio_seconds)
    return (result.has_speech, result.audio_seconds)


def _assemble(transcript_text: str, visual_text: str, probe: MediaProbe, notes: list[str]) -> str:
    """Put the two readings together, transcript first.

    The order and the labels carry the weighting — the same device already used
    for a photographed document, where the OCR transcription leads and the visual
    description follows as context. Speech is what someone deliberately said;
    a still is an inference from one frame.
    """
    blocks: list[str] = []
    kind = "video" if probe.has_video else "audio recording"
    header = f"[{kind}, duration {int(probe.duration_seconds // 60)}m {int(probe.duration_seconds % 60):02d}s]"
    blocks.append(header)
    if transcript_text.strip():
        blocks.append(f"[Spoken transcript — reliable]\n{transcript_text.strip()}")
    if visual_text.strip():
        blocks.append(f"[Visual description of sampled frames — context, less reliable]\n{visual_text.strip()}")
    if notes:
        blocks.append("[Reading notes]\n" + "\n".join(f"- {note}" for note in notes))
    return "\n\n".join(blocks)


def read_audio_video(
    path: Path,
    *,
    original_filename: str | None = None,
    source_folder: str | None = None,
) -> AVReadResult:
    """Read one audio or video file into text, as cheaply as its content allows."""
    probe = probe_media(path)
    if not probe.ok:
        return AVReadResult(reason=probe.reason or "unreadable_media")
    if probe.duration_seconds <= 0:
        # A container with no measurable duration cannot be sampled or billed
        # sensibly; guessing a length would be inventing the cost.
        return AVReadResult(reason="zero_duration")

    notes: list[str] = []
    transcript = TranscriptResult(reason="no_audio_track")

    with tempfile.TemporaryDirectory(prefix="procrafiler-av-") as workspace:
        work = Path(workspace)

        if probe.has_audio:
            # Listen to a few seconds first, on a recording long enough for it to
            # pay. Music, ambience and wind all transcribe to nothing, and paying
            # for the whole file to learn that is the single most wasteful thing
            # this reader can do.
            speech_expected = True
            probe_seconds = 0
            if probe.duration_seconds >= MIN_PROBE_DURATION:
                speech_expected, probe_seconds = has_any_speech(path, probe.duration_seconds, work)

            if not speech_expected:
                transcript = TranscriptResult(audio_seconds=probe_seconds)
                notes.append(
                    "the audio contains no recognisable speech (music, noise or silence) — "
                    f"heard {PROBE_WINDOWS} samples across the recording instead of "
                    "transcribing all of it"
                )
            else:
                cap = max_transcribe_seconds()
                truncated = probe.duration_seconds > cap
                speed = transcribe_speed()
                audio_path = work / "audio.mp3"
                if extract_audio(
                    path, audio_path, max_seconds=cap if truncated else None, speed=speed
                ):
                    if truncated:
                        notes.append(
                            f"only the first {cap // 60} minutes were transcribed "
                            f"(recording is {int(probe.duration_seconds // 60)} minutes)"
                        )
                    # The timestamps come back on the SPED-UP clock. Putting them
                    # back on the recording's own clock has to happen here, before
                    # anything reasons about when things were said — the frame
                    # planner would otherwise cut every still from the wrong moment.
                    transcript = rescale_to_source_time(transcribe(audio_path), speed)
                    transcript.audio_seconds += probe_seconds
                    if transcript.reason:
                        notes.append(f"transcription unavailable ({transcript.reason})")
                    elif not transcript.has_speech:
                        # Verified against the live API: music or noise returns 200
                        # with an empty transcript. Saying so is useful content — it
                        # tells the analysis this is not a spoken document.
                        notes.append("the audio contains no recognisable speech (music, noise or silence)")
                else:
                    notes.append("the audio track could not be extracted")
        else:
            notes.append("this file has no audio track")

        visual_text = ""
        frames_analysed = 0
        transcribed_frames = 0
        if probe.has_video:
            budget = min(frame_budget(probe.duration_seconds), MAX_FRAMES_HARD_CAP)
            # Two of the budget are always the ends; only the rest is worth asking
            # a model to choose, so we never pay for a selection we cannot use.
            highlights = select_highlights(
                transcript, probe.duration_seconds, wanted=max(0, budget - 2)
            )
            timestamps = plan_frame_timestamps(probe.duration_seconds, highlights, budget=budget)
            frames = extract_frames(path, timestamps, work / "frames")
            if not frames:
                notes.append("no frame could be extracted from the video")

            # Extracting is free; LOOKING is not. A filmed interview is visually
            # static, so a dozen stills of it are a dozen paid descriptions of the
            # same man in the same chair — measured at 12 frames for 4 distinct
            # scenes on a real recording. Comparing the images locally, before any
            # call, removes that waste without touching quality.
            distinct = select_distinct([perceptual_hash(frame) for frame in frames])
            if len(distinct) < len(frames):
                notes.append(
                    f"{len(frames) - len(distinct)} of {len(frames)} sampled frames "
                    "showed the same scene and were not sent for reading"
                )

            descriptions: list[str] = []
            for index in distinct:
                frame = frames[index]
                at = timestamps[index] if index < len(timestamps) else 0.0
                # The SAME reading a photograph gets: describe it, and when the
                # model reports a written document, re-read it with OCR. A slide,
                # a filmed page or a whiteboard therefore comes back transcribed
                # rather than described — the loss #116 fixed for photos, which
                # this path would otherwise have reintroduced.
                read = read_visual(
                    frame, original_filename=original_filename, source_folder=source_folder
                )
                if read.is_readable and read.text:
                    descriptions.append(f"at {int(at // 60)}m{int(at % 60):02d}s — {read.text.strip()}")
                    frames_analysed += 1
                    if read.used_ocr:
                        transcribed_frames += 1
            visual_text = "\n".join(descriptions)

    transcript_text = format_transcript(transcript) if transcript.has_speech else ""
    if not transcript_text and not visual_text:
        # Nothing was read. Better to hand it to the user than to file a document
        # on the strength of its filename alone.
        return AVReadResult(
            duration_seconds=probe.duration_seconds,
            transcript=transcript,
            reason="nothing_readable",
            notes=notes,
        )

    return AVReadResult(
        text=_assemble(transcript_text, visual_text, probe, notes),
        duration_seconds=probe.duration_seconds,
        transcript=transcript,
        frames_analysed=frames_analysed,
        transcribed_frames=transcribed_frames,
        notes=notes,
    )
