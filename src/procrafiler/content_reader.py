"""Local content extraction — the first step of the IA-first reading chain.

ProcraFiler's whole point is to read each file's content and derive its name
and category from that reading (never from the filename). This module does the
part that needs no AI and no network: it pulls out text we can already read
locally, and otherwise tells the caller which AI reader is needed.

Decisions, per the file's media type (from `taxonomy.dispatch_for_filename`):
- text            -> read the text directly.
- pdf             -> if it has a usable text layer, extract it (a "readable"
                     PDF); if not, it's a scan and needs OCR.
- image           -> needs an AI vision reader.
- everything else -> not handled by local extraction (yet).

This module performs no AI call. The reader_hint tells the (future) AI layer
which model to use: OCR for scanned PDFs, vision for images.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

# pypdf logs warnings (e.g. "invalid pdf header", "EOF marker not found") for
# malformed PDFs. We already handle those by routing the file to OCR, so the
# warnings are redundant noise — keep our output clean.
logging.getLogger("pypdf").setLevel(logging.ERROR)


# A readable PDF has a real text layer; a scan yields little or nothing. Stray
# artifacts can leak a few characters, so require a small minimum before we
# call a PDF "readable" rather than "needs OCR". Heuristic, intentionally low.
_MIN_EXTRACTABLE_CHARS = 16

READER_HINT_OCR = "ocr"
READER_HINT_VISION = "vision"


@dataclass(frozen=True)
class ContentExtraction:
    """Outcome of trying to read a file's content locally.

    `text` is the extracted text when we could read it without AI (text files,
    readable PDFs). When `needs_ai_reader` is True, `reader_hint` says which AI
    reader the downstream layer should use (`ocr` / `vision`). `reason` is a
    stable machine-readable label for logs and tests.
    """

    media_type: str
    text: str | None
    needs_ai_reader: bool
    reader_hint: str | None
    reason: str


def _read_text_file(path: Path) -> ContentExtraction:
    data = path.read_bytes()
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        return ContentExtraction("text", text="", needs_ai_reader=False, reader_hint=None, reason="empty")
    return ContentExtraction("text", text=text, needs_ai_reader=False, reader_hint=None, reason="text_extracted")


def _read_pdf(path: Path) -> ContentExtraction:
    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages:
            extracted = page.extract_text() or ""
            if extracted:
                parts.append(extracted)
        text = "\n".join(parts)
    except (PyPdfError, OSError, ValueError):
        # Corrupt, encrypted, or otherwise unreadable by pypdf — fall back to
        # OCR, which works from the rendered image rather than the text layer.
        return ContentExtraction(
            "pdf", text=None, needs_ai_reader=True, reader_hint=READER_HINT_OCR, reason="pdf_extract_error"
        )

    if len(text.strip()) >= _MIN_EXTRACTABLE_CHARS:
        return ContentExtraction("pdf", text=text, needs_ai_reader=False, reader_hint=None, reason="text_extracted")

    # No usable text layer: this is a scanned/image PDF, send it to OCR.
    return ContentExtraction(
        "pdf", text=None, needs_ai_reader=True, reader_hint=READER_HINT_OCR, reason="scanned_pdf_needs_ocr"
    )


def extract_text_content(path: Path, media_type: str) -> ContentExtraction:
    """Extract locally-readable text, or report which AI reader is needed.

    `media_type` comes from `taxonomy.dispatch_for_filename`. This call never
    performs an AI request; it only reads what can be read on disk.
    """
    if media_type == "text":
        return _read_text_file(path)
    if media_type == "pdf":
        return _read_pdf(path)
    if media_type == "image":
        return ContentExtraction(
            "image", text=None, needs_ai_reader=True, reader_hint=READER_HINT_VISION, reason="image_needs_vision"
        )
    return ContentExtraction(
        media_type, text=None, needs_ai_reader=False, reader_hint=None, reason="unsupported_local_extraction"
    )
