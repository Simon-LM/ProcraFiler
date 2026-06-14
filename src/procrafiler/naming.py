from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def _slugify_stem(stem: str) -> str:
    normalized = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    # Underscores are allowed inside a stem (naming templates use them, e.g.
    # CV_LOUVEL-Simon) but any run mixing them with other separators collapses
    # to ONE underscore, so a stem can never contain `__` — that pair is
    # reserved as the timestamp-prefix separator.
    normalized = re.sub(r"[^A-Za-z0-9_]+", "-", normalized)
    normalized = re.sub(r"-*_[-_]*", "_", normalized)
    normalized = normalized.strip("-_")
    return normalized or "file"


def _strip_leading_date(stem: str) -> str:
    """Drop a redundant date that LEADS the stem — the timestamp prefix already
    carries the date. Only a month-precision date (YYYY-MM or YYYY-MM-DD) is
    removed; a bare year (YYYY) can be part of the identity (e.g.
    Recensement-population_2026), so it is kept."""
    stripped = re.sub(r"^\d{4}-\d{2}(?:-\d{2})?[-_]+", "", stem)
    return stripped or stem


def sanitize_filename_stem(stem: str) -> str:
    return _strip_leading_date(_slugify_stem(stem))


def build_timestamped_filename(original_name: str, now_utc: datetime | None = None) -> str:
    """Build an MVP-compliant UTC-prefixed filename.

    Format: YYYY-MM-DD_HH-mm-ss__Original-Name.ext
    """
    dt = now_utc or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    path = Path(original_name)
    timestamp = dt.strftime("%Y-%m-%d_%H-%M-%S")
    # Route through sanitize_filename_stem so a redundant leading date is stripped
    # uniformly (the timestamp prefix below already carries the date).
    safe_stem = sanitize_filename_stem(path.stem)
    return f"{timestamp}__{safe_stem}{path.suffix}"
