"""What a music or film file says about itself, without listening or looking.

This is the media zone's whole reading. Everywhere else in ProcraFiler the app
opens the file and reads its CONTENT; here it deliberately does not, and the
difference is not a limitation but the point.

Transcribing an album buys nothing — there are no words, and when there are they
are lyrics, which say what the song is rather than what the file is. Describing
frames of a film costs a great deal to learn what the title already said. And no
model available here can recognise a piece of music from its sound at all.

What a media file *does* carry is written down: the tags a ripper or an editor
wrote into it (ID3, Vorbis comments, MP4 atoms, container tags), its own name, and
above all the name of the folder it sits in — for an album or a series that folder
name is usually the single most informative thing in the whole tree, and it is
free to read.

So this module collects that text and hands it on. The AI still runs — on these
words, to make sense of them — but **not one byte of audio, image or video leaves
the machine**.

Everything here is best-effort: a file with no tags at all is the normal case for
a WAV, and it yields a description built from its name and its folder alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from procrafiler.media_tools import _run, ffmpeg_available

_PROBE_TIMEOUT = 30

# Tag keys worth showing, and the label each gets. ffprobe normalises most
# containers onto these lowercase names, so one table covers MP3/ID3, FLAC and OGG
# Vorbis comments, MP4/M4A atoms and Matroska. Keys absent from a file are simply
# absent from the description — never invented, never defaulted.
#
# Ordered deliberately: album and artist before title, because the question being
# answered is "what IS this file", and for a track the album answers it better
# than the track name does.
_TAG_LABELS: tuple[tuple[str, str], ...] = (
    ("album", "album"),
    ("album_artist", "album artist"),
    ("artist", "artist"),
    ("title", "title"),
    ("track", "track number"),
    ("disc", "disc"),
    ("date", "date"),
    ("year", "year"),
    ("genre", "genre"),
    ("composer", "composer"),
    ("performer", "performer"),
    ("publisher", "publisher"),
    ("label", "label"),
    ("copyright", "copyright"),
    ("description", "description"),
    ("comment", "comment"),
    ("show", "show"),
    ("season_number", "season"),
    ("episode_id", "episode"),
    ("synopsis", "synopsis"),
    ("language", "language"),
)

# A tag value long enough to be a pasted booklet or an embedded lyric sheet is
# truncated: it would dominate the prompt without saying more about what the file
# IS. Generous enough to keep a real synopsis whole.
_MAX_TAG_CHARS = 400


@dataclass(frozen=True)
class MediaDescription:
    """Everything known about a media file without opening its content."""

    filename: str
    folder: str = ""
    parent_folder: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    technical: dict[str, str] = field(default_factory=dict)

    @property
    def has_tags(self) -> bool:
        return bool(self.tags)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:_MAX_TAG_CHARS]


def _probe_json(path: Path) -> dict[str, Any]:
    """ffprobe's view of the container: tags plus stream shape. {} on any problem."""
    if not ffmpeg_available() or not path.is_file():
        return {}
    code, out, _err = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration,bit_rate,format_long_name:format_tags"
            ":stream=codec_type,codec_name,sample_rate,channels,width,height:stream_tags",
            "-of", "json", str(path),
        ],
        timeout=_PROBE_TIMEOUT,
    )
    if code != 0:
        return {}
    try:
        payload = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _collect_tags(payload: dict[str, Any]) -> dict[str, str]:
    """The written tags, lowercased and de-duplicated across format and streams.

    Container tags and per-stream tags are merged because different formats put
    the same fact in different places — MP4 keeps the title on the format, some
    Matroska writers on the stream.
    """
    raw: dict[str, str] = {}
    for source in ((payload.get("format") or {}).get("tags"), *(
        (stream.get("tags") if isinstance(stream, dict) else None)
        for stream in (payload.get("streams") or [])
    )):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            cleaned = _clean(value)
            if cleaned is not None:
                raw.setdefault(str(key).strip().lower(), cleaned)

    found: dict[str, str] = {}
    for key, label in _TAG_LABELS:
        if key in raw:
            found[label] = raw[key]
    return found


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def _collect_technical(payload: dict[str, Any]) -> dict[str, str]:
    """The shape of the file — duration, codecs, resolution, sample rate.

    Not decoration: it separates kinds of media that names alone confuse. A
    two-hour H.264 file is a film; a four-minute 44.1 kHz stereo one is a track; a
    32-bit float export is a working session rather than a finished master.
    """
    technical: dict[str, str] = {}
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = 0.0
    if duration > 0:
        technical["duration"] = _format_duration(duration)

    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        if kind == "video" and "video" not in technical:
            bits = [str(stream.get("codec_name") or "video")]
            width, height = stream.get("width"), stream.get("height")
            if width and height:
                bits.append(f"{width}x{height}")
            technical["video"] = ", ".join(bits)
        elif kind == "audio" and "audio" not in technical:
            bits = [str(stream.get("codec_name") or "audio")]
            if stream.get("sample_rate"):
                bits.append(f"{stream['sample_rate']} Hz")
            channels = stream.get("channels")
            if channels:
                bits.append("mono" if channels == 1 else "stereo" if channels == 2 else f"{channels} channels")
            technical["audio"] = ", ".join(bits)
    return technical


def _image_metadata(path: Path) -> dict[str, str]:
    """EXIF/IPTC description fields of an image — never its pixels.

    Only the fields that SAY something about the subject (a caption, an author, a
    camera) are kept. Everything geometric is left out: it is the one thing a media
    fiche has no use for.
    """
    try:
        from PIL import Image, ExifTags  # optional dep
    except ImportError:
        return {}
    wanted = {
        "ImageDescription": "description",
        "Artist": "artist",
        "Copyright": "copyright",
        "Make": "camera maker",
        "Model": "camera",
        "XPTitle": "title",
        "XPComment": "comment",
    }
    found: dict[str, str] = {}
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return {}
            by_name = {ExifTags.TAGS.get(tag, str(tag)): value for tag, value in exif.items()}
            for key, label in wanted.items():
                raw = by_name.get(key)
                if isinstance(raw, bytes):  # the XP* fields are UTF-16 byte strings
                    raw = raw.decode("utf-16-le", "ignore").rstrip("\x00")
                cleaned = _clean(raw)
                if cleaned is not None:
                    found[label] = cleaned
    except Exception:
        return {}
    return found


def describe_media(path: Path, library_root: Path | None = None) -> MediaDescription:
    """Everything known about a media file without reading its content.

    Never raises: an unreadable or exotic file yields a description built from its
    name and its folder, which is still enough to catalog it.
    """
    folder = ""
    parent = ""
    try:
        parts = path.parent.parts
        folder = parts[-1] if parts else ""
        parent = parts[-2] if len(parts) > 1 else ""
        if library_root is not None:
            relative = path.parent.relative_to(library_root).parts
            folder = relative[-1] if relative else ""
            parent = relative[-2] if len(relative) > 1 else ""
    except (ValueError, OSError):
        pass

    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("jpg", "jpeg", "png", "tif", "tiff", "webp", "heic", "heif"):
        return MediaDescription(filename=path.name, folder=folder, parent_folder=parent,
                                tags=_image_metadata(path))

    payload = _probe_json(path)
    return MediaDescription(
        filename=path.name,
        folder=folder,
        parent_folder=parent,
        tags=_collect_tags(payload),
        technical=_collect_technical(payload),
    )


def format_media_description(description: MediaDescription) -> str:
    """The text handed to the analysis call in place of a document's content.

    It states plainly that nothing was read, because the model must not answer as
    though it had heard the music or seen the film — an invented summary of an
    album it never listened to is exactly the failure this zone exists to avoid.
    """
    lines = [
        "This is a MEDIA file (music, film or recording) that was NOT opened: no audio "
        "was listened to and no image was looked at. Everything below is written "
        "metadata and folder naming. Identify what this file is FROM THESE WORDS ALONE, "
        "and say so plainly when they are not enough — never describe sound or pictures "
        "you have not been given.",
        "",
        f"- filename: {description.filename}",
    ]
    if description.folder:
        lines.append(
            f"- folder holding it: {description.folder}   (for a music album or a series "
            "this folder name is often the most reliable identification available)"
        )
    if description.parent_folder:
        lines.append(f"- folder above that: {description.parent_folder}")

    if description.tags:
        lines.append("- metadata written in the file:")
        lines += [f"    · {label}: {value}" for label, value in description.tags.items()]
    else:
        lines.append(
            "- metadata written in the file: NONE (common for WAV and for files stripped "
            "on export) — the names above are all there is"
        )

    if description.technical:
        lines.append("- technical shape:")
        lines += [f"    · {label}: {value}" for label, value in description.technical.items()]
    return "\n".join(lines)
