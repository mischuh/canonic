"""Canon-wide exceptions."""

from __future__ import annotations


class CanonError(Exception):
    """Base exception for all canon errors."""


class ReadOnlyViolation(CanonError):
    """Raised when a non-read-only SQL statement is submitted to run_read_only_sql()."""


class SchemaMismatch(CanonError):
    """Raised when declared schema does not match the live source during probe validation."""


class ConnectionError(CanonError):  # noqa: A001 — intentionally shadows builtin in this namespace
    """Raised when a connector cannot establish or maintain a connection."""
