from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def _slugify_stem(stem: str) -> str:
    normalized = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-")
    return normalized or "file"


def sanitize_filename_stem(stem: str) -> str:
    return _slugify_stem(stem)


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
    safe_stem = _slugify_stem(path.stem)
    return f"{timestamp}__{safe_stem}{path.suffix}"
