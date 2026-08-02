"""What the file itself says about when it was made — for every kind of file.

Almost every format carries a creation date somewhere in its own bytes: a photo
has EXIF, a video has a container tag, a PDF has `/CreationDate`, a Word or
LibreOffice file has an XML property. They were being ignored everywhere except
photos, so a PDF's own production date — often the only date a scan has — never
reached anything.

This module collects them. It does not rank them and it does not decide.

**Why collection and decision are separated.** These dates say when the FILE was
produced, which is not the same question as what date the DOCUMENT bears. A
letter written in March and scanned in July has a July `/CreationDate` and a
March date printed on it; a holiday photo has only its EXIF; a video downloaded
twice has neither. Which one is "the document's date" depends on what the
document turns out to be — so it is answered by the model that just read it, not
by a rule written here. What this module provides is the evidence that model
judges, phrased plainly enough to be judged.

The extraction is the only part that differs per format. Everything downstream —
how the evidence is presented, and the fallback ladder when the model returns no
date at all — is shared by every file type.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from procrafiler.media_tools import container_creation_time

# A malformed PDF makes pypdf log "invalid pdf header" / "EOF marker not found".
# Here that is the expected answer, not a problem — the file simply has no date
# we can read — so the warnings are noise. Same silencing as `content_reader`,
# repeated because either module may be the first one imported.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# What each source actually attests, in the words the analysis prompt shows the
# model. Deliberately factual, never prescriptive: the point is to say what the
# timestamp IS so the model can weigh it against the content, not to tell it
# which one wins. "Produced" and "written to disk" are doing real work here — a
# scan's PDF date is the day it was scanned, and an mtime is very often just the
# day the file was downloaded.
_SOURCE_MEANING: dict[str, str] = {
    "exif": "the camera's capture date (EXIF) — when this photo was taken",
    "container": "the recording's creation date, written by the device or the editor",
    "pdf": "the PDF's own production date — when this file was generated, which for a "
           "scan is the day it was scanned, not the day the paper was written",
    "ooxml": "the office document's creation date, as stored by the editor",
    "odf": "the office document's creation date, as stored by the editor",
    "mtime": "when the file was last written to disk — often merely when it was "
             "downloaded, copied or exported, so it is the weakest of these",
}


@dataclass(frozen=True)
class DateHint:
    """One dated fact found about a file, with what it attests."""

    source: str
    value: datetime

    @property
    def meaning(self) -> str:
        return _SOURCE_MEANING.get(self.source, self.source)

    @property
    def day(self) -> str:
        return self.value.strftime("%Y-%m-%d")


def _as_utc(value: datetime) -> datetime:
    """Naive timestamps are read as UTC, like the rest of the pipeline."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _exif_capture_datetime(path: Path) -> datetime | None:
    """EXIF DateTimeOriginal (or DateTime) of an image, or None.

    Real metadata written by the camera, so it is worth far more than a date a
    vision model believes it can see in the picture. EXIF carries no timezone;
    the naive value is treated as UTC. Any problem — no Pillow, no EXIF,
    unparseable — yields None.
    """
    try:
        from PIL import Image  # optional dep; absence just disables EXIF dating
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            raw = None
            try:
                # DateTimeOriginal (36867) lives in the Exif sub-IFD (0x8769).
                raw = exif.get_ifd(0x8769).get(36867)
            except Exception:
                raw = None
            if not isinstance(raw, str) or not raw.strip():
                raw = exif.get(306)  # DateTime (fallback)
            if not isinstance(raw, str) or not raw.strip():
                return None
            return datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _pdf_creation_date(path: Path) -> datetime | None:
    """A PDF's `/CreationDate` from its document info dictionary, or None.

    pypdf is already a dependency (it is what reads the text layer) and it parses
    the PDF date syntax `D:20260312093000+01'00'` for us. Absent from many PDFs,
    and wrong in a few — producers copy it when a file is re-saved — which is
    exactly why it is offered as evidence rather than taken as the answer.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        info = reader.metadata
        if info is None:
            return None
        found = info.creation_date
    except Exception:
        return None
    return _as_utc(found) if isinstance(found, datetime) else None


# The XML namespaces the two office families use for their creation date.
_OOXML_CORE = "docProps/core.xml"
_DCTERMS_CREATED = "{http://purl.org/dc/terms/}created"
_ODF_META = "meta.xml"
_ODF_CREATION_DATE = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}creation-date"


def _zipped_xml_date(path: Path, member: str, tag: str) -> datetime | None:
    """Read one ISO date out of one XML member of a zip-based document.

    Both office families are zip archives holding XML — OOXML (.docx/.xlsx/.pptx)
    keeps `dcterms:created` in docProps/core.xml, ODF (.odt/.ods/.odp) keeps
    `meta:creation-date` in meta.xml. Same shape, so one reader serves both, and
    no new dependency: zipfile and ElementTree are in the standard library.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read(member)
        element = ElementTree.fromstring(raw)
    except Exception:
        return None
    node = element.find(f".//{tag}")
    text = (node.text or "").strip() if node is not None else ""
    if not text:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _ooxml_created(path: Path) -> datetime | None:
    return _zipped_xml_date(path, _OOXML_CORE, _DCTERMS_CREATED)


def _odf_created(path: Path) -> datetime | None:
    return _zipped_xml_date(path, _ODF_META, _ODF_CREATION_DATE)


# Extension -> extractor. Keyed on the EXTENSION, not the media type: .docx and
# .odt are both dispatched as "text" for reading purposes but store their date in
# two different places, and .pdf is its own reader class. The media type answers
# "how do I read the bytes", which is a different question.
_BY_EXTENSION: dict[str, str] = {
    "pdf": "pdf",
    "docx": "ooxml", "xlsx": "ooxml", "pptx": "ooxml",
    "odt": "odf", "ods": "odf", "odp": "odf",
}

def _container_date(path: Path) -> datetime | None:
    """Indirection on purpose: the name is resolved at call time, so the ffprobe
    hop stays substitutable from a test without the offline suite growing a real
    video file."""
    return container_creation_time(path)


_EXTRACTORS = {
    "exif": _exif_capture_datetime,
    "container": _container_date,
    "pdf": _pdf_creation_date,
    "ooxml": _ooxml_created,
    "odf": _odf_created,
}


def embedded_date(path: Path, media_type: str | None = None) -> DateHint | None:
    """The date the file's own format records, or None when it records none.

    Never raises: every extractor swallows its own failures, because a file that
    will not open is a file with no embedded date — not a reason to stop filing
    it.
    """
    extension = path.suffix.lower().lstrip(".")
    source = _BY_EXTENSION.get(extension)
    if source is None and media_type == "image":
        source = "exif"
    if source is None and media_type in ("video", "audio"):
        source = "container"
    if source is None:
        return None
    found = _EXTRACTORS[source](path)
    return DateHint(source=source, value=_as_utc(found)) if found is not None else None


def modified_date(path: Path) -> DateHint | None:
    try:
        return DateHint(source="mtime", value=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        return None


def date_evidence(path: Path, media_type: str | None = None) -> list[DateHint]:
    """Every dated fact the filesystem and the file's own format can offer.

    Strongest first — the format's own record before the filesystem's mtime —
    because that is the order a reader scans a list in, not because the order
    decides anything.
    """
    found = [embedded_date(path, media_type), modified_date(path)]
    return [hint for hint in found if hint is not None]


def format_date_evidence(hints: list[DateHint]) -> str:
    """The evidence block shown to the analysis prompt. Empty when there is none.

    Each line is a date and what it attests. No instruction on what to do with
    them: the model has just read the document and is the only party that knows
    whether the content's own date, one of these, or none of them is the date
    this document bears.
    """
    if not hints:
        return ""
    lines = [
        "- timestamps carried by the file itself (they date the FILE, which may or may "
        "not be the date the document bears):"
    ]
    lines += [f"    · {hint.day} — {hint.meaning}" for hint in hints]
    return "\n".join(lines)
