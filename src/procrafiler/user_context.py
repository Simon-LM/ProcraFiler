"""Optional user-context file: free-text notes about the user (passions, work,
places, identity) injected into the AI prompts to disambiguate classification
(e.g. a hobby vs work) and anchor naming.

It is PERSONAL data: read-only, never written to the action log, never committed
(the real `context.txt` / `context.md` is gitignored; only a `.example` template
ships). Absent or empty → returns None and the pipeline behaves exactly as before.

Lookup order (first existing, non-empty file wins):
  1. ``PROCRAFILER_CONTEXT_FILE`` (explicit path), if set
  2. ``./context.txt`` then ``./context.md`` (the repo / working dir — where the
     template lives)
  3. ``<PROCRAFILER_CONFIG_HOME>/context.txt`` then ``.../context.md``
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Bound the context injected into every prompt: enough for a useful profile,
# small enough to keep token cost predictable.
MAX_CONTEXT_CHARS = 2000


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("PROCRAFILER_CONTEXT_FILE")
    if explicit:
        candidates.append(Path(explicit))
    cwd = Path.cwd()
    candidates.extend([cwd / "context.txt", cwd / "context.md"])
    config_home = Path(
        os.environ.get("PROCRAFILER_CONFIG_HOME", str(Path.home() / ".config" / "procrafiler"))
    )
    candidates.extend([config_home / "context.txt", config_home / "context.md"])

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _clean(text: str) -> str:
    """Drop the template's guidance so only the user's real notes reach the model:
    HTML comments (``<!-- … -->``) and lines that start with ``#`` (the .txt
    comment convention). Section labels like ``[Identité]`` and content are kept."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "\n".join(kept).strip()


def active_context_path() -> Path | None:
    """The file `load_user_context` would currently read (first existing
    candidate in the lookup order), or None when none exists."""
    for path in _candidate_paths():
        if path.is_file():
            return path
    return None


def default_context_write_path() -> Path:
    """Where `setup-context` writes the context: the explicit
    `PROCRAFILER_CONTEXT_FILE` if set, else the per-user config home
    (`<PROCRAFILER_CONFIG_HOME>/context.md`)."""
    explicit = os.environ.get("PROCRAFILER_CONTEXT_FILE")
    if explicit:
        return Path(explicit)
    config_home = Path(
        os.environ.get("PROCRAFILER_CONFIG_HOME", str(Path.home() / ".config" / "procrafiler"))
    )
    return config_home / "context.md"


def load_user_context() -> str | None:
    """Return the cleaned user-context text (capped at ``MAX_CONTEXT_CHARS``), or
    None when no context file exists or it carries no real content."""
    for path in _candidate_paths():
        try:
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cleaned = _clean(raw)
        if cleaned:
            return cleaned[:MAX_CONTEXT_CHARS]
    return None
