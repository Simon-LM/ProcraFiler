from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from procrafiler.naming import sanitize_filename_stem


BASE_LIBRARY_DIRECTORIES: tuple[tuple[str, ...], ...] = (
    ("Personnel", "Documents"),
    ("Professionnel", "Documents"),
    ("Administratif",),
    ("Banque",),
    ("Telephonie",),
    ("Internet",),
    ("Personnel", "Medias", "Images"),
    ("Personnel", "Medias", "Videos"),
    ("Personnel", "Medias", "Audio"),
    ("Personnel", "Archives"),
    ("Revue_Manuelle",),
)


# Interim destination used while there is no AI classifier yet.
#
# The extension only tells us *how to read* a file (its media type), never
# *where it belongs* (its category). The category is the job of AI
# classification from the file content, which is not implemented yet. Until
# then, every readable file lands here for a human (or, later, the AI) to
# categorize. This is NOT an extension->category mapping — it is the explicit
# absence of a category decision.
INTERIM_LIBRARY_DIR: tuple[str, ...] = ("Revue_Manuelle",)


# Extension -> media type. This is a TECHNICAL DISPATCH only: it decides which
# processing capability can read the bytes (PDF extraction, OCR, image
# analysis, plain-text reading, ...). It must NEVER be used to decide the
# destination category. See docs/spec-mvp-v1.md §9-10.
_GROUPED_MEDIA_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pdf", ("pdf",)),
    ("text", ("txt", "md", "rtf", "doc", "docx", "odt", "epub")),
    ("office", ("xls", "xlsx", "ods", "csv", "tsv", "ppt", "pptx", "odp")),
    (
        "image",
        (
            "jpg",
            "jpeg",
            "png",
            "gif",
            "bmp",
            "tif",
            "tiff",
            "webp",
            "heic",
            "heif",
            "svg",
            "psd",
            "xcf",
            "raw",
            "dng",
            "cr2",
            "nef",
            "arw",
            "raf",
            "rw2",
            "orf",
            "srw",
        ),
    ),
    ("video", ("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp")),
    ("audio", ("mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "aiff")),
    (
        "archive",
        (
            "zip",
            "rar",
            "7z",
            "tar",
            "gz",
            "bz2",
            "xz",
            "tar.gz",
            "tar.bz2",
            "tar.xz",
            "tgz",
            "tbz2",
            "txz",
        ),
    ),
)


_EXTENSION_TO_MEDIA_TYPE: dict[str, str] = {
    ext: media_type
    for media_type, exts in _GROUPED_MEDIA_TYPES
    for ext in exts
}


@dataclass(frozen=True)
class DispatchDecision:
    """Result of the technical dispatch step.

    `media_type` is the reader class that can handle the file (pdf / text /
    office / image / video / audio / archive), or None when the file cannot be
    dispatched at all (no extension, or an extension we don't recognize). It is
    deliberately NOT a destination folder — see the module docstring on
    INTERIM_LIBRARY_DIR.
    """

    media_type: str | None
    reason: str | None
    matched_extension: str | None

    @property
    def can_dispatch(self) -> bool:
        return self.media_type is not None


def ensure_base_library_directories(library_root: Path) -> None:
    for relative_dir in BASE_LIBRARY_DIRECTORIES:
        (library_root / Path(*relative_dir)).mkdir(parents=True, exist_ok=True)


def category_label(relative_dir: tuple[str, ...]) -> str:
    """Render a category path as a single label (e.g. ('Personnel','Documents') -> 'Personnel/Documents')."""
    return "/".join(relative_dir)


def classifiable_categories() -> tuple[tuple[str, ...], ...]:
    """Semantic categories the AI may choose from — every base directory except
    the interim review bucket (which is the fallback, not a real category)."""
    return tuple(d for d in BASE_LIBRARY_DIRECTORIES if d != INTERIM_LIBRARY_DIR)


def category_from_label(label: str) -> tuple[str, ...] | None:
    """Map a category label back to its relative_dir tuple, or None if unknown."""
    for relative_dir in classifiable_categories():
        if category_label(relative_dir) == label:
            return relative_dir
    return None


def existing_category_paths(library_root: Path) -> list[str]:
    """List every folder currently under the base categories, as labels.

    Shown to the AI so it can REUSE an existing folder instead of inventing a
    near-duplicate. The base categories themselves are included; the interim
    review bucket is not (it is not a real category)."""
    paths: list[str] = []
    for base in classifiable_categories():
        base_dir = library_root / Path(*base)
        if not base_dir.exists():
            continue
        paths.append(category_label(base))
        for directory in sorted(p for p in base_dir.rglob("*") if p.is_dir()):
            paths.append("/".join(directory.relative_to(library_root).parts))
    return paths


def normalize_category_path(label: str, max_depth: int) -> tuple[str, ...] | None:
    """Validate an AI-proposed folder path into a safe relative_dir, or None.

    Rules (P2a):
    - The path MUST start with one of the existing base categories — the AI may
      not create a new top-level category.
    - Segments below the base are new/existing subfolders; their names are
      normalized (slugified) so `Impôts` / `impots` / `Impots ` collapse to one
      folder instead of three.
    - Total depth is capped at `max_depth` (a safety net; 0 means uncapped).
    Returns the validated relative_dir tuple, or None when no base matches
    (caller then routes the file to manual review).
    """
    segments = [s.strip() for s in label.split("/") if s.strip()]
    if not segments:
        return None

    matched_base: tuple[str, ...] | None = None
    for base in sorted(classifiable_categories(), key=len, reverse=True):
        if tuple(segments[: len(base)]) == base:
            matched_base = base
            break
    if matched_base is None:
        return None

    sub_segments: tuple[str, ...] = tuple(
        slug for slug in (sanitize_filename_stem(seg) for seg in segments[len(matched_base):]) if slug
    )
    full = matched_base + sub_segments
    if max_depth > 0:
        full = full[:max_depth]
    return full


def dispatch_for_filename(filename: str) -> DispatchDecision:
    """Decide which reader/media type can process a file, from its extension.

    This answers only "what kind of bytes is this, and therefore which reader
    handles it" — never "where does it belong". Files with no extension or an
    unrecognized extension cannot be dispatched and must go to manual review.
    """
    suffixes = [suffix.lower().lstrip(".") for suffix in Path(filename).suffixes if suffix]
    if not suffixes:
        return DispatchDecision(media_type=None, reason="no_extension", matched_extension=None)

    if len(suffixes) >= 2:
        compound_extension = f"{suffixes[-2]}.{suffixes[-1]}"
        media_type = _EXTENSION_TO_MEDIA_TYPE.get(compound_extension)
        if media_type is not None:
            return DispatchDecision(media_type=media_type, reason=None, matched_extension=compound_extension)

    extension = suffixes[-1]
    media_type = _EXTENSION_TO_MEDIA_TYPE.get(extension)
    if media_type is None:
        return DispatchDecision(media_type=None, reason="unknown_extension", matched_extension=extension)

    return DispatchDecision(media_type=media_type, reason=None, matched_extension=extension)
