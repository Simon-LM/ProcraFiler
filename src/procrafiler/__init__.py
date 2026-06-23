"""ProcraFiler package."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

__all__ = ["__version__"]

try:
    # The version comes from the installed package metadata, which setuptools-scm
    # derives from the latest git tag — so it always matches the release, with no
    # hardcoded constant to drift.
    __version__ = _pkg_version("procrafiler")
except PackageNotFoundError:  # running from a bare source tree, not installed
    __version__ = "0+unknown"
