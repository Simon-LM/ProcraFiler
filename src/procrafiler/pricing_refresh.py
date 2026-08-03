"""Fetching current prices from the companion repository.

The packaged price table is dated the day the release was cut, and it never moves
after that. Mistral changes its rates every few months, so an installation left
alone for a year quietly prices every run against figures that stopped being true
long ago — while still printing them with a date that makes them look checked.

The companion repository (`docs/ai-pricing-source.md`) publishes a `pricing.json`
that a scheduled job re-verifies weekly and a human reviews before publication.
This module goes and gets it.

Four rules, and they are what makes a network call acceptable inside a filing tool:

**Never blocking.** A short timeout, every failure swallowed. No network, a
firewall, a DNS hole, GitHub down — the run proceeds on the table it already has.
Filing documents must never wait on a price.

**At most weekly.** Prices move a few times a year; checking more often is traffic
for nothing. The attempt is recorded whether or not it succeeded, so an offline
machine tries once a week rather than on every single run.

**Validated before it is trusted.** The downloaded file is parsed and bounds-checked
by `pricing` before it can replace anything. A truncated download, a redirect to an
HTML error page, or a file whose figures are absurd is discarded and the previous
copy stays in place — a stale price the user can see the age of beats a wrong one.

**Switchable off.** `PROCRAFILER_PRICING_REFRESH=off` and the app never touches the
network. Someone running an air-gapped machine, or who simply does not want it,
should not have to trust a promise about how short the timeout is.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from procrafiler.pricing import PriceTable, _parse_table  # type: ignore[reportMissingImports]

DEFAULT_PRICING_URL = "https://raw.githubusercontent.com/Simon-LM/ai-pricing/main/pricing.json"

# Long enough for a slow connection, short enough that nobody notices it on a
# broken one. This is the number the "never blocking" promise rests on.
FETCH_TIMEOUT_SECONDS = 5

REFRESH_INTERVAL = timedelta(days=7)

# A pricing file is a few kilobytes. Anything wildly larger is not the file we
# asked for, and reading it into memory before finding that out is the mistake.
MAX_DOWNLOAD_BYTES = 256 * 1024

CACHE_FILENAME = "pricing.cached.json"
STAMP_FILENAME = "pricing.checked"


def refresh_enabled() -> bool:
    return os.environ.get("PROCRAFILER_PRICING_REFRESH", "").strip().lower() not in (
        "off", "0", "false", "no",
    )


def pricing_url() -> str:
    return os.environ.get("PROCRAFILER_PRICING_URL", "").strip() or DEFAULT_PRICING_URL


def _stamp_file(config_dir: Path) -> Path:
    return config_dir / STAMP_FILENAME


def cache_file(config_dir: Path) -> Path:
    return config_dir / CACHE_FILENAME


def last_checked(config_dir: Path) -> datetime | None:
    try:
        raw = _stamp_file(config_dir).read_text("utf-8").strip()
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None


def is_due(config_dir: Path, *, now: datetime | None = None) -> bool:
    """True when a week has passed since the last ATTEMPT — success or failure.

    Recording failed attempts too is what stops an offline machine from trying on
    every run for the rest of its life.
    """
    if not refresh_enabled():
        return False
    checked = last_checked(config_dir)
    if checked is None:
        return True
    return (now or datetime.now(timezone.utc)) - checked >= REFRESH_INTERVAL


def _record_attempt(config_dir: Path, now: datetime | None = None) -> None:
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        _stamp_file(config_dir).write_text(
            (now or datetime.now(timezone.utc)).isoformat(), encoding="utf-8"
        )
    except OSError:
        pass  # an unwritable config dir means we retry next run; never an error


def fetch_price_table(url: str | None = None, *, timeout: int = FETCH_TIMEOUT_SECONDS) -> tuple[PriceTable | None, str]:
    """Download and validate one price table. Returns (table, raw text).

    Never raises. A failure yields `(None, "")`, and the caller keeps whatever it
    was already using.
    """
    request = Request(url or pricing_url(), headers={"User-Agent": "ProcraFiler"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 — fixed https URL
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (URLError, OSError, ValueError):
        return (None, "")
    if len(raw) > MAX_DOWNLOAD_BYTES:
        return (None, "")

    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return (None, "")
    table = _parse_table(payload, "downloaded")
    if table is None:
        return (None, "")
    # Parsing succeeds on a file whose figures were all rejected as implausible —
    # every model survives with its prices stripped to None, which is a table that
    # answers "I cannot price this" for everything. Accepting it would REPLACE a
    # working copy with a useless one, so the whole download is refused instead.
    # Found by serving a file with a price a thousand times too large.
    if not any(price.is_priceable for price in (table.models or {}).values()):
        return (None, "")
    return (table, text)


def refresh_if_due(config_dir: Path | None, *, now: datetime | None = None) -> PriceTable | None:
    """Update the cached price table when a week has passed. Returns the new table,
    or None when nothing was fetched — which is the ordinary case, not a problem.

    Called before a run so a forecast quotes current rates. Everything it can go
    wrong at is already an ordinary outcome; the run never learns the difference.
    """
    if config_dir is None or not is_due(config_dir, now=now):
        return None
    _record_attempt(config_dir, now)

    table, text = fetch_price_table()
    if table is None:
        return None
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        cache_file(config_dir).write_text(text, encoding="utf-8")
    except OSError:
        return None  # could not store it; next week will try again
    return table
