from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from procrafiler.naming import sanitize_filename_stem


# The base library tree shipped with the app (spec §10). It is organized by life
# CONTEXT (Personal / Work) and then by SUBJECT — never by file format. Names are
# English (the universal business architecture); a user's own inbox folder names
# stay in their language and are only a hint. The AI files into these and creates
# anything finer itself (e.g. Clients/<name>, Insurance/Water-Damage-2025,
# Personal/Trip-Spain-2025) — those instances are NOT part of the base tree.
BASE_LIBRARY_DIRECTORIES: tuple[tuple[str, ...], ...] = (
    ("Personal",),
    ("Personal", "Administrative"),
    ("Personal", "Administrative", "Identity"),
    ("Personal", "Administrative", "Taxes"),
    ("Personal", "Administrative", "Banking"),
    ("Personal", "Administrative", "Insurance"),
    ("Personal", "Administrative", "Health"),
    ("Personal", "Administrative", "Housing"),
    ("Personal", "Administrative", "Utilities"),
    ("Personal", "Administrative", "Telecom"),
    ("Personal", "Administrative", "Vehicle"),
    ("Personal", "Education"),
    ("Personal", "Hobbies"),
    ("Personal", "Social-media"),
    ("Personal", "Misc"),
    ("Personal", "Archive"),
    ("Work",),
    ("Work", "Employment"),
    ("Work", "Employment", "Administrative"),
    ("Work", "Employment", "Payslips"),
    ("Work", "Business"),
    ("Work", "Business", "Administrative"),
    ("Work", "Business", "Invoices"),
    ("Work", "Business", "Expenses"),
    ("Work", "Business", "Clients"),
    ("Work", "Misc"),
    ("Work", "Archive"),
    ("Media",),
    ("Media", "Music"),
    ("Media", "Films"),
    ("Manual_Review",),
)


# ARCHIVE folders are USER zones, not AI targets. They are scaffolded (visible, so
# the user can drop backups / snapshots / old folders in them) but excluded from
# the categories the AI may choose (like Manual_Review) — archiving is the user's
# deliberate act, never an AI decision, and this avoids re-creating a catch-all
# magnet. rescan treats everything under an Archive folder as a PRESERVE ZONE:
# indexed for search, but never renamed/moved/reorganized (same as a VCS repo).
# (Their purpose is documented in README.md; we keep the folders empty like the
# rest of the base tree rather than littering each one with a note file.)
ARCHIVE_BASE_DIRECTORIES: tuple[tuple[str, ...], ...] = (
    ("Personal", "Archive"),
    ("Work", "Archive"),
)


# MEDIA folders hold music albums and films the user files BY HAND. Like Archive
# they are a user zone the AI never files into, and nothing inside is ever renamed
# or moved — but the resemblance stops there, and the difference is the whole point.
#
# An Archive folder holds DOCUMENTS: they are read, and being able to search inside
# them is exactly why the zone exists. A media file is not read at all. There is
# nothing to gain from transcribing an album or describing every frame of a film,
# it would cost a great deal, and the app has no way to recognise a piece of music
# from its sound anyway.
#
# So the AI is not excluded here — it is MOVED. It stops reading the content and
# reads what is written AROUND it: the file's own metadata (ID3, Vorbis comments,
# MP4 atoms, container tags), its name, and the name of the folder holding it,
# which for an album or a series is usually the most informative thing available.
# Not one byte of audio, image or video leaves the machine. See `media_metadata`.
#
# `Media` sits at the top level rather than under Personal/Work because it answers
# a different question from those two: they say what a document is ABOUT, `Media`
# says how a file is TREATED. An album is neither personal nor professional, it is
# an album — and a rule about processing, buried under a subject branch, becomes
# invisible.
MEDIA_BASE_DIRECTORIES: tuple[tuple[str, ...], ...] = (
    ("Media",),
)


def is_in_media_zone(relative_parts: tuple[str, ...]) -> bool:
    """True when a library-relative path lives under the media zone."""
    return any(tuple(relative_parts[: len(base)]) == base for base in MEDIA_BASE_DIRECTORIES)


# Translations / synonyms of the (English) base-folder segment names, per language
# code. Used so search finds a document by its category in the user's language
# (e.g. "passion" or "loisirs" → the `Hobbies` folder) without any AI. The base
# tree is a small FIXED set, so this is curated once. Only French is provided for
# now; adding another language is just another inner key — `folder_synonyms`
# returns [] for any segment/language not listed, so English category search
# always works regardless.
BASE_FOLDER_TRANSLATIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "Personal": {"fr": ("personnel", "perso", "privé")},
    "Administrative": {"fr": ("administratif", "administration", "démarches")},
    "Identity": {"fr": ("identité", "papiers")},
    "Taxes": {"fr": ("impôts", "fiscal", "taxes")},
    "Banking": {"fr": ("banque", "bancaire", "compte")},
    "Insurance": {"fr": ("assurance", "assurances", "mutuelle")},
    "Health": {"fr": ("santé", "médical", "médecin")},
    "Housing": {"fr": ("logement", "habitation", "maison")},
    "Utilities": {"fr": ("énergie", "électricité", "gaz", "eau", "factures")},
    "Telecom": {"fr": ("téléphone", "internet", "mobile", "box")},
    "Vehicle": {"fr": ("véhicule", "voiture", "auto")},
    "Education": {"fr": ("éducation", "études", "école", "scolaire", "diplôme")},
    "Hobbies": {"fr": ("loisirs", "passions", "passion", "hobby", "hobbies")},
    "Social-media": {"fr": ("réseaux", "sociaux", "réseau", "social", "médias")},
    "Misc": {"fr": ("divers", "autres")},
    "Employment": {"fr": ("emploi", "salarié", "travail")},
    "Payslips": {"fr": ("paie", "salaire", "bulletins")},
    "Business": {"fr": ("entreprise", "activité", "société", "business")},
    "Invoices": {"fr": ("factures", "facture")},
    "Expenses": {"fr": ("dépenses", "frais")},
    "Clients": {"fr": ("clients", "client")},
    "Work": {"fr": ("travail", "professionnel", "pro", "boulot")},
    "Media": {"fr": ("médias", "media", "multimédia")},
    "Music": {"fr": ("musique", "musiques", "album", "albums", "morceaux")},
    "Films": {"fr": ("films", "film", "cinéma", "vidéos", "séries")},
}


def folder_synonyms(segment: str, language: str) -> tuple[str, ...]:
    """Synonyms/translations of a base-folder segment in `language` (e.g.
    "Hobbies","fr" → loisirs/passions/…). Empty when not curated."""
    return BASE_FOLDER_TRANSLATIONS.get(segment, {}).get(language, ())


def is_in_archive(relative_parts: tuple[str, ...]) -> bool:
    """True when a library-relative path lives under one of the Archive folders."""
    return any(tuple(relative_parts[: len(base)]) == base for base in ARCHIVE_BASE_DIRECTORIES)


# The safe catch-all destination. A file lands here when its content can't be
# read at all (unreadable/unsupported) or when the AI is genuinely uncertain —
# never a guessed category. It is the explicit ABSENCE of a category decision,
# not an extension->category mapping. (Photos that ARE classifiable go to their
# subject folder like any document; the format only tells us how to read them.)
INTERIM_LIBRARY_DIR: tuple[str, ...] = ("Manual_Review",)


# Extension -> media type. This is a TECHNICAL DISPATCH only: it decides which
# processing capability can read the bytes (PDF extraction, OCR, image
# analysis, plain-text reading, ...). It must NEVER be used to decide the
# destination category. See docs/spec-mvp-v1.md §9-10.
_GROUPED_MEDIA_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pdf", ("pdf",)),
    ("text", ("txt", "md", "rtf", "doc", "docx", "odt", "epub", "srt", "sh")),
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


# Image extensions the vision model can actually decode (Mistral accepts JPEG,
# PNG, WEBP, GIF, MPO, HEIF, AVIF, BMP, TIFF). Other "image" formats — editor
# files (.xcf, .psd), camera RAW (.cr2, .nef…), vector (.svg) — are still
# media_type "image" (so EXIF capture dates keep working), but are NOT sent to
# vision: that call only errors and wastes a request. They are timestamped and
# catalogued without an AI read.
VISION_READABLE_EXTENSIONS: frozenset[str] = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif", "avif", "mpo"}
)


def is_vision_readable(extension: str) -> bool:
    """True when the vision model can decode this image extension (dot optional)."""
    return extension.lower().lstrip(".") in VISION_READABLE_EXTENSIONS


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
    """Render a category path as a single label (e.g. ('Personal','Administrative') -> 'Personal/Administrative')."""
    return "/".join(relative_dir)


_MEDIA_SUBTREE: frozenset[tuple[str, ...]] = frozenset(
    d for d in BASE_LIBRARY_DIRECTORIES if is_in_media_zone(d)
)
_NON_CLASSIFIABLE: frozenset[tuple[str, ...]] = frozenset(
    (INTERIM_LIBRARY_DIR, *ARCHIVE_BASE_DIRECTORIES, *_MEDIA_SUBTREE)
)


def classifiable_categories() -> tuple[tuple[str, ...], ...]:
    """Semantic categories the AI may choose from — every base directory EXCEPT
    the interim review bucket (the fallback, not a real category), the Archive
    folders and the Media zone (user-only zones; the AI never files there).

    Media is excluded for the same reason as Archive but with more force: the
    files there are deliberately never read, so the model has no basis on which to
    send anything into it, and a run must never move a document there by mistake."""
    return tuple(d for d in BASE_LIBRARY_DIRECTORIES if d not in _NON_CLASSIFIABLE)


def category_from_label(label: str) -> tuple[str, ...] | None:
    """Map a category label back to its relative_dir tuple, or None if unknown."""
    for relative_dir in classifiable_categories():
        if category_label(relative_dir) == label:
            return relative_dir
    return None


def base_category_for(relative_dir: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the LONGEST base category that is a prefix of `relative_dir`, or
    None when it sits under no base (e.g. Manual_Review). Lets a caller tell an
    ENTITY/affair subfolder (deeper than its base) from a bare base."""
    for base in sorted(classifiable_categories(), key=len, reverse=True):
        if tuple(relative_dir[: len(base)]) == base:
            return base
    return None


def existing_category_paths(library_root: Path) -> list[str]:
    """List every folder currently under the base categories, as labels.

    Shown to the AI so it can REUSE an existing folder instead of inventing a
    near-duplicate. The base categories themselves are included; the interim
    review bucket is not (it is not a real category). De-duplicated and sorted —
    the base tree is nested, so a directory can be reached via several bases."""
    labels: set[str] = set()
    for base in classifiable_categories():
        base_dir = library_root / Path(*base)
        if not base_dir.exists():
            continue
        labels.add(category_label(base))
        for directory in (p for p in base_dir.rglob("*") if p.is_dir()):
            labels.add("/".join(directory.relative_to(library_root).parts))
    return sorted(labels)


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


def normalize_review_path(label: str, max_depth: int) -> tuple[str, ...] | None:
    """Validate a path a USER chose during `review`, allowing a brand-new root.

    Same slugify+depth-cap rules as `normalize_category_path`, with one
    difference: the user may create a new top-level category here (this is the
    ONLY place new roots are allowed — the AI never can). To avoid forking a
    near-duplicate root, a typed first segment that matches an existing base
    case-insensitively snaps onto that base's canonical casing. Returns the
    validated relative_dir tuple, or None when the label is empty.
    """
    segments = [s.strip() for s in label.split("/") if s.strip()]
    if not segments:
        return None

    lowered = [s.lower() for s in segments]
    matched_base: tuple[str, ...] | None = None
    for base in sorted(classifiable_categories(), key=len, reverse=True):
        if tuple(lowered[: len(base)]) == tuple(part.lower() for part in base):
            matched_base = base
            break

    if matched_base is not None:
        rest = segments[len(matched_base):]
        sub = tuple(slug for slug in (sanitize_filename_stem(seg) for seg in rest) if slug)
        full = matched_base + sub
    else:
        # Brand-new root category — slugify every segment, including the first.
        full = tuple(slug for slug in (sanitize_filename_stem(seg) for seg in segments) if slug)

    if not full:
        return None
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
