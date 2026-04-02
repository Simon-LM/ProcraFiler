from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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


_GROUPED_EXTENSION_ROUTES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("Personnel", "Documents"), ("pdf", "txt", "md", "rtf", "doc", "docx", "odt", "epub")),
    (("Professionnel", "Documents"), ("xls", "xlsx", "ods", "csv", "tsv", "ppt", "pptx", "odp")),
    (
        ("Personnel", "Medias", "Images"),
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
    (("Personnel", "Medias", "Videos"), ("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp")),
    (("Personnel", "Medias", "Audio"), ("mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "aiff")),
    (
        ("Personnel", "Archives"),
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


_EXTENSION_TO_ROUTE: dict[str, tuple[str, ...]] = {
    ext: route
    for route, exts in _GROUPED_EXTENSION_ROUTES
    for ext in exts
}


@dataclass(frozen=True)
class RouteDecision:
    relative_dir: tuple[str, ...] | None
    reason: str | None
    matched_extension: str | None

    @property
    def needs_manual_review(self) -> bool:
        return self.relative_dir is None


def ensure_base_library_directories(library_root: Path) -> None:
    for relative_dir in BASE_LIBRARY_DIRECTORIES:
        (library_root / Path(*relative_dir)).mkdir(parents=True, exist_ok=True)


def decide_route_for_filename(filename: str) -> RouteDecision:
    suffixes = [suffix.lower().lstrip(".") for suffix in Path(filename).suffixes if suffix]
    if not suffixes:
        return RouteDecision(relative_dir=None, reason="no_extension", matched_extension=None)

    if len(suffixes) >= 2:
        compound_extension = f"{suffixes[-2]}.{suffixes[-1]}"
        route = _EXTENSION_TO_ROUTE.get(compound_extension)
        if route is not None:
            return RouteDecision(relative_dir=route, reason=None, matched_extension=compound_extension)

    extension = suffixes[-1]
    route = _EXTENSION_TO_ROUTE.get(extension)
    if route is None:
        return RouteDecision(relative_dir=None, reason="unknown_extension", matched_extension=extension)

    return RouteDecision(relative_dir=route, reason=None, matched_extension=extension)
