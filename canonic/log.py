"""Central logging configuration for the canonic logger hierarchy."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time

#: Set by callers (e.g. ``CanonicService.query``) to a short per-call id so every
#: log record emitted while it's in flight — across service/resolver/pipeline
#: loggers — can be correlated back to the same call via ``_QueryIdFilter``.
query_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("query_id", default=None)

_TEXT_FORMATTER = logging.Formatter(
    fmt="%(asctime)s %(levelname)-8s %(name)s [qid=%(qid)s]: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
_TEXT_FORMATTER.converter = time.gmtime


class _JsonFormatter(logging.Formatter):
    """One JSON object per record — machine-parseable, still a single line."""

    def __init__(self) -> None:
        super().__init__()
        self.converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "qid": getattr(record, "qid", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


_JSON_FORMATTER = _JsonFormatter()


class _QueryIdFilter(logging.Filter):
    """Stamp each record with the current call's id from ``query_id_var``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.qid = query_id_var.get() or "-"
        return True


def configure_logging(
    level: str = "WARNING", file: str | None = None, format: str = "text"
) -> None:
    """Configure the canonic logger hierarchy.

    Targets the ``canonic`` root logger only — third-party library loggers are
    unaffected. Safe to call multiple times; existing handlers are replaced.

    Args:
        level: A standard logging level name (DEBUG/INFO/WARNING/ERROR/CRITICAL).
            Case-insensitive. Unknown names fall back to WARNING.
        file: Path to a log file (appended). When ``None``, records go to stderr.
            Never stdout: on stdio MCP transport, stdout carries the JSON-RPC
            stream, so logs must stay on stderr or a file.
        format: ``"text"`` (default) or ``"json"`` for one JSON object per line.
    """
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.WARNING

    canonic_logger = logging.getLogger("canonic")
    canonic_logger.setLevel(numeric)
    canonic_logger.handlers.clear()

    handler: logging.Handler
    if file is not None:
        handler = logging.FileHandler(file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(_JSON_FORMATTER if format == "json" else _TEXT_FORMATTER)
    handler.addFilter(_QueryIdFilter())
    canonic_logger.addHandler(handler)
    canonic_logger.propagate = False  # prevent double-emission to root logger


def _effective_log_params(
    config_level: str, config_file: str | None, config_format: str = "text"
) -> tuple[str, str | None, str]:
    """Return log level, file path, and format with env var overrides applied.

    Environment variables ``CANONIC_LOG_LEVEL``, ``CANONIC_LOG_FILE``, and
    ``CANONIC_LOG_FORMAT`` take precedence over whatever the config file specifies.
    """
    level = os.environ.get("CANONIC_LOG_LEVEL", config_level)
    file = os.environ.get("CANONIC_LOG_FILE", config_file)
    format = os.environ.get("CANONIC_LOG_FORMAT", config_format)
    return level, file, format
