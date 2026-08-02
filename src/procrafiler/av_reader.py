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
from procrafiler.ai_reader import read_with_vision  # type: ignore[reportMissingImports]
from procrafiler.ai_transcribe import TranscriptResult, format_transcript, transcribe  # type: ignore[reportMissingImports]
from procrafiler.frame_sampling import frame_budget, plan_frame_timestamps  # type: ignore[reportMissingImports]
from procrafiler.media_tools import (  # type: ignore[reportMissingImports]
    DEFAULT_MAX_TRANSCRIBE_SECONDS,
    MediaProbe,
    extract_audio,
    extract_frames,
    probe_media,
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
            cap = max_transcribe_seconds()
            truncated = probe.duration_seconds > cap
            audio_path = work / "audio.mp3"
            if extract_audio(path, audio_path, max_seconds=cap if truncated else None):
                if truncated:
                    notes.append(
                        f"only the first {cap // 60} minutes were transcribed "
                        f"(recording is {int(probe.duration_seconds // 60)} minutes)"
                    )
                transcript = transcribe(audio_path)
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
            descriptions: list[str] = []
            for index, frame in enumerate(frames):
                at = timestamps[index] if index < len(timestamps) else 0.0
                read = read_with_vision(
                    frame, original_filename=original_filename, source_folder=source_folder
                )
                if read.text and read.text.strip():
                    descriptions.append(f"at {int(at // 60)}m{int(at % 60):02d}s — {read.text.strip()}")
                    frames_analysed += 1
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
        notes=notes,
    )
