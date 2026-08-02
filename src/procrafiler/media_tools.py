"""Looking inside a video or an audio file, locally and for free.

Everything here shells out to **ffmpeg / ffprobe**. That is a deliberate choice
over a Python binding: ffmpeg is the one tool that reads essentially every
container and codec a user will ever drop in, it is packaged on every Linux
distribution, and a subprocess cannot take the interpreter down with it when a
file turns out to be malformed.

The consequence is that ffmpeg may be **missing**, and this module is written so
that missing it is an ordinary answer rather than an error: `probe_media` returns
a reason, the caller routes the file to manual review, and nothing crashes. A user
who never drops a video should not have to install anything.

Three questions get answered before a single paid call is made — how long is it,
does it have any audio at all, and can it be decoded — because each one changes
what the AV reader will spend money on, and two of them can make it spend nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# A malformed file can make ffmpeg sit and spin. These bound the damage; they are
# generous enough that a large legitimate file on a slow disk still succeeds.
_PROBE_TIMEOUT = 30
_EXTRACT_TIMEOUT = 900

# Transcription is billed by the second, so a forgotten four-hour recording could
# quietly become the most expensive file the user ever dropped. Only this much of
# it is transcribed; the rest is reported as truncated rather than silently paid
# for. Raised via PROCRAFILER_MAX_TRANSCRIBE_SECONDS.
DEFAULT_MAX_TRANSCRIBE_SECONDS = 45 * 60


@dataclass(frozen=True)
class MediaProbe:
    """What ffprobe could tell us about a file, without decoding all of it."""

    duration_seconds: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    ok: bool = False
    reason: str | None = None

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60.0


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(command: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return (127, b"", b"not found")
    except subprocess.TimeoutExpired:
        return (124, b"", b"timeout")
    except OSError as exc:  # pragma: no cover - defensive
        return (1, b"", str(exc).encode())
    return (completed.returncode, completed.stdout, completed.stderr)


def probe_media(path: Path) -> MediaProbe:
    """Duration and stream inventory. Never raises."""
    if not ffmpeg_available():
        return MediaProbe(reason="ffmpeg_not_installed")
    if not path.is_file():
        return MediaProbe(reason="file_missing")

    code, out, _err = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path),
        ],
        timeout=_PROBE_TIMEOUT,
    )
    if code != 0:
        return MediaProbe(reason="unreadable_media")

    try:
        payload = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return MediaProbe(reason="unreadable_media")

    streams = payload.get("streams") if isinstance(payload, dict) else None
    codec_types = {
        str(stream.get("codec_type"))
        for stream in (streams or [])
        if isinstance(stream, dict)
    }
    raw_duration = ((payload.get("format") or {}) if isinstance(payload, dict) else {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = 0.0
    if duration != duration or duration < 0:  # NaN guard: some streams report it
        duration = 0.0

    # A container ffprobe accepts but that holds neither audio nor video is not a
    # media file in any useful sense — treat it as unreadable rather than paying
    # to look at nothing.
    if not codec_types & {"audio", "video"}:
        return MediaProbe(reason="no_media_stream")

    return MediaProbe(
        duration_seconds=duration,
        has_audio="audio" in codec_types,
        has_video="video" in codec_types,
        ok=True,
    )


def extract_audio(source: Path, destination: Path, *, max_seconds: int | None = None) -> bool:
    """Pull the audio track out as a small mono 16 kHz MP3.

    Downmixed and downsampled on purpose: speech recognition gains nothing from
    stereo or from 48 kHz, and the upload is what costs time on a slow connection.
    A two-hour recording becomes a few megabytes.
    """
    if not ffmpeg_available() or not source.is_file():
        return False
    command = ["ffmpeg", "-v", "error", "-y", "-i", str(source)]
    if max_seconds and max_seconds > 0:
        command += ["-t", str(int(max_seconds))]
    command += ["-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(destination)]

    code, _out, _err = _run(command, timeout=_EXTRACT_TIMEOUT)
    # ffmpeg can exit 0 having written nothing at all (a video whose "audio track"
    # is an empty stream), so success is judged on the artefact, not the code.
    return code == 0 and destination.is_file() and destination.stat().st_size > 0


def container_creation_time(path: Path) -> datetime | None:
    """When the container says the recording was made, or None.

    A video has no EXIF, but MP4/MOV carry `creation_time` in their metadata and
    Matroska carries `DateUTC` — the same class of fact as a photo's capture date:
    written by the device, not interpreted from the content. Measured on real
    files: present on a camera/editor export, absent from a WebM download, since
    re-encoding and most download pipelines strip it.
    """
    if not ffmpeg_available() or not path.is_file():
        return None
    code, out, _err = _run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags",
         "-of", "json", str(path)],
        timeout=_PROBE_TIMEOUT,
    )
    if code != 0:
        return None
    try:
        payload = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return None

    tags: dict[str, Any] = dict((payload.get("format") or {}).get("tags") or {})
    for stream in payload.get("streams") or []:
        if isinstance(stream, dict):
            tags.update(stream.get("tags") or {})

    for key in ("creation_time", "DateUTC", "date"):
        raw = tags.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip().replace("Z", "+00:00")
        for parse in (
            lambda t: datetime.fromisoformat(t),
            lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
            lambda t: datetime.strptime(t, "%Y-%m-%d"),
        ):
            try:
                found = parse(text)
            except ValueError:
                continue
            return found if found.tzinfo else found.replace(tzinfo=timezone.utc)
    return None


def perceptual_hash(path: Path) -> int | None:
    """A 64-bit average-hash of an image, computed with ffmpeg. None if unreadable.

    ffmpeg scales the frame to 8x8 greyscale and hands back 64 raw bytes; each bit
    of the hash says whether that cell is brighter than the frame's mean. Two
    frames of the same static shot then differ by a handful of bits, two different
    scenes by dozens.

    No new dependency: ffmpeg is already required to have got this far, and a
    Python imaging library would be a heavy addition for sixty-four bytes.
    """
    code, out, _err = _run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=8:8,format=gray",
         "-f", "rawvideo", "-"],
        timeout=_PROBE_TIMEOUT,
    )
    if code != 0 or len(out) != 64:
        return None
    average = sum(out) / 64
    value = 0
    for index, cell in enumerate(out):
        if cell > average:
            value |= 1 << index
    return value


def extract_frames(source: Path, timestamps: list[float], out_dir: Path) -> list[Path]:
    """Grab one JPEG per timestamp, each seeked to independently.

    One ffmpeg call per frame rather than one pass with a filter: a single bad
    timestamp (past the end, or on a corrupt region) then costs us that one frame
    instead of the whole set. Frames are the input to a paid vision call, so
    partial success is worth much more than all-or-nothing.
    """
    if not ffmpeg_available() or not source.is_file():
        return []
    out_dir.mkdir(parents=True, exist_ok=True)

    frames: list[Path] = []
    for index, position in enumerate(timestamps):
        target = out_dir / f"frame_{index:03d}.jpg"
        code, _out, _err = _run(
            [
                "ffmpeg", "-v", "error", "-y",
                # -ss BEFORE -i is the fast seek: ffmpeg jumps in the container
                # instead of decoding from zero, which matters on a long file.
                "-ss", f"{max(0.0, position):.3f}",
                "-i", str(source),
                "-frames:v", "1",
                # Downscale wide frames: a 4K still costs vision tokens by its
                # pixel count, and nothing in a filing decision needs 4K.
                "-vf", "scale='min(1280,iw)':-2",
                "-q:v", "4",
                str(target),
            ],
            timeout=_EXTRACT_TIMEOUT,
        )
        if code == 0 and target.is_file() and target.stat().st_size > 0:
            frames.append(target)
    return frames
