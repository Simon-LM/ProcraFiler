from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
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


# The app's own timestamp prefix, `YYYY-MM-DD_HH-MM-SS__`. Stripped FIRST (before
# slugify collapses the `__`) so re-naming a file that already carries it — e.g.
# rescan ingesting a hand-placed file the user named in our own format — does not
# double the prefix (`…__00-00-00__…`). Matched on the RAW stem.
_TIMESTAMP_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}__")


# Dates a filename may carry. Deliberately narrow: these are the machine-written
# forms people and cameras actually produce, and each one is unambiguous.
# `DD-MM-YYYY` is accepted only when the first group cannot be a month, because
# 03-04-2026 is the third of April to half the world and the fourth of March to
# the other half — and a silently wrong date is worse than no date.
_FILENAME_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?P<y>\d{4})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<d>1[3-9]|2\d|3[01])[-_.](?P<m>\d{2})[-_.](?P<y>\d{4})(?!\d)"),
    re.compile(r"(?<!\d)(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"),
)
# A plausible document date. The upper bound is what keeps a version number or a
# random id that happens to parse (`20991231`) from dating a file to the far
# future, where it would sort above everything the user owns.
_EARLIEST_FILENAME_YEAR = 1900


def date_from_filename(name: str, *, today: date | None = None) -> date | None:
    """A date written in the filename, or None.

    Filenames are the one place a date survives when the content has none: a video
    is the clear case — no EXIF to read, and nothing spoken that states a day — but
    it is equally true of a scan named by hand. The app already RECOGNISED such
    dates, in `_strip_leading_date`, purely in order to delete them; the
    information was being thrown away rather than read.

    Conservative by design. It returns a date only when the digits can mean
    nothing else, and never one in the future.
    """
    limit = (today or datetime.now(timezone.utc).date())
    for pattern in _FILENAME_DATE_PATTERNS:
        for match in pattern.finditer(name):
            try:
                found = date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
            except ValueError:
                continue  # 2026-13-45 is digits, not a date
            if found.year < _EARLIEST_FILENAME_YEAR or found > limit:
                continue
            return found
    return None


def _strip_leading_date(stem: str) -> str:
    """Drop a redundant date that LEADS the stem — the timestamp prefix already
    carries the date. Only a month-precision date (YYYY-MM or YYYY-MM-DD) is
    removed; a bare year (YYYY) can be part of the identity (e.g.
    Recensement-population_2026), so it is kept."""
    stripped = re.sub(r"^\d{4}-\d{2}(?:-\d{2})?[-_]+", "", stem)
    return stripped or stem


# Longest stem we keep. A filesystem name maxes out at 255 BYTES (ext4, most
# others), and the stem is only part of the final name: the app's own
# `YYYY-MM-DD_HH-MM-SS__` prefix costs 21, the extension a few more, and a
# `__1`-style deduplication suffix may be appended after that. 180 leaves generous
# room for all of it while keeping names readable.
#
# This cap exists because the stem can come from an AI: a model that answers the
# "name" field with a whole descriptive sentence instead of a title would otherwise
# produce a filename the filesystem REFUSES (ENAMETOOLONG), failing the placement
# of a document that is otherwise perfectly fine. Truncating is always better than
# refusing to file the user's document.
MAX_STEM_CHARS = 180


def _truncate_stem(stem: str) -> str:
    """Cap the stem at MAX_STEM_CHARS, cutting on a separator when one is close by
    so the result still reads as words rather than a chopped fragment."""
    if len(stem.encode("utf-8")) <= MAX_STEM_CHARS:
        return stem
    clipped = stem[:MAX_STEM_CHARS]
    # Prefer the last separator in the final quarter, to avoid cutting mid-word.
    cut = max(clipped.rfind("-"), clipped.rfind("_"))
    if cut >= MAX_STEM_CHARS * 3 // 4:
        clipped = clipped[:cut]
    return clipped.strip("-_") or "file"


def sanitize_filename_stem(stem: str) -> str:
    stem = _TIMESTAMP_PREFIX_RE.sub("", stem)
    return _truncate_stem(_strip_leading_date(_slugify_stem(stem)))


def has_timestamp_prefix(name: str) -> bool:
    """True when `name` already carries the app's `YYYY-MM-DD_HH-MM-SS__` prefix —
    so rescan can ENSURE the prefix on a file that lacks one without re-stamping
    (and re-dating) one that already has it."""
    return bool(_TIMESTAMP_PREFIX_RE.match(name))


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
