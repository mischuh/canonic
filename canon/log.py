"""Central logging configuration for the canon logger hierarchy."""

from __future__ import annotations

import logging
import os
import sys

_FORMATTER = logging.Formatter(
    fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


def configure_logging(level: str = "WARNING", file: str | None = None) -> None:
    """Configure the canon logger hierarchy.

    Targets the ``canon`` root logger only — third-party library loggers are
    unaffected. Safe to call multiple times; existing handlers are replaced.

    Args:
        level: A standard logging level name (DEBUG/INFO/WARNING/ERROR/CRITICAL).
            Case-insensitive. Unknown names fall back to WARNING.
        file: Path to a log file (appended). When ``None``, records go to stderr.
    """
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.WARNING

    canon_logger = logging.getLogger("canon")
    canon_logger.setLevel(numeric)
    canon_logger.handlers.clear()

    handler: logging.Handler
    if file is not None:
        handler = logging.FileHandler(file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(_FORMATTER)
    canon_logger.addHandler(handler)
    canon_logger.propagate = False  # prevent double-emission to root logger


def _effective_log_params(config_level: str, config_file: str | None) -> tuple[str, str | None]:
    """Return log level and file path with env var overrides applied.

    Environment variables ``CANON_LOG_LEVEL`` and ``CANON_LOG_FILE`` take
    precedence over whatever the config file specifies.
    """
    level = os.environ.get("CANON_LOG_LEVEL", config_level)
    file = os.environ.get("CANON_LOG_FILE", config_file)
    return level, file
