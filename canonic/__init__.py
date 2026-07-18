"""Canonic — the open context layer for data agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("canonic")
except PackageNotFoundError:  # pragma: no cover — running from an uninstalled source tree
    __version__ = "unknown"

__all__ = ["__version__"]
