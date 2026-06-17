"""Canon-wide exceptions and the canonical error registry (SPEC E7+E8 §6.1)."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


class ErrorCode(StrEnum):
    """Canonical error codes shared across capabilities and serving surfaces.

    The string value is the stable wire/JSON identifier; ``EXIT_CODES`` maps each
    code to its headless exit value.
    """

    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    UNREACHABLE = "unreachable"
    AMBIGUOUS_JOIN_PATH = "ambiguous_join_path"
    UNSUPPORTED_MEASURE = "unsupported_measure"
    FANOUT_UNSAFE = "fanout_unsafe"
    GUARDRAIL_BLOCK = "guardrail_block"
    VALIDATION_FAILED = "validation_failed"
    ASSERTION_FAILED = "assertion_failed"
    READ_ONLY_VIOLATION = "read_only_violation"
    SCHEMA_MISMATCH = "schema_mismatch"
    CONNECTION_ERROR = "connection_error"
    # Additive contradiction gate for headless strict mode (SPEC-E4 §5.4); does not
    # touch the frozen serving contract.
    CONTRADICTION = "contradiction"


EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.UNRESOLVED: 2,
    ErrorCode.AMBIGUOUS: 3,
    ErrorCode.UNREACHABLE: 4,
    ErrorCode.AMBIGUOUS_JOIN_PATH: 5,
    ErrorCode.UNSUPPORTED_MEASURE: 6,
    ErrorCode.FANOUT_UNSAFE: 7,
    ErrorCode.GUARDRAIL_BLOCK: 8,
    ErrorCode.VALIDATION_FAILED: 9,
    ErrorCode.ASSERTION_FAILED: 10,
    ErrorCode.READ_ONLY_VIOLATION: 11,
    ErrorCode.SCHEMA_MISMATCH: 12,
    ErrorCode.CONNECTION_ERROR: 13,
    ErrorCode.CONTRADICTION: 14,
}


class CanonError(Exception):
    """Base exception for all canon errors.

    Subclasses set ``code`` to a registry entry; ``exit_code`` then yields the
    headless exit value. Errors without a registry code (internal/unexpected) use
    exit ``1`` by convention.
    """

    code: ErrorCode | None = None

    def __init__(self, message: str = "", *, candidates: Sequence[Any] | None = None) -> None:
        """Structured error: a message plus optional candidate list (SPEC-E5 §4).

        ``candidates`` carries the alternatives an upstream caller can act on
        programmatically (e.g. the competing bindings behind an ``AMBIGUOUS`` result),
        so errors are never free text.
        """
        super().__init__(message)
        self.candidates: tuple[Any, ...] = tuple(candidates) if candidates is not None else ()

    @property
    def exit_code(self) -> int:
        """Headless exit value for this error per the canonical registry (§6.1)."""
        if self.code is None:
            return 1
        return EXIT_CODES[self.code]


class Unresolved(CanonError):
    """Metric name matches no active binding (E5)."""

    code = ErrorCode.UNRESOLVED


class Ambiguous(CanonError):
    """Name matches more than one active binding; candidates returned (E5)."""

    code = ErrorCode.AMBIGUOUS


class Unreachable(CanonError):
    """Dimension/filter has no join path to the metric source (E5)."""

    code = ErrorCode.UNREACHABLE


class AmbiguousJoinPath(CanonError):
    """More than one valid join path; an explicit path is required (E5)."""

    code = ErrorCode.AMBIGUOUS_JOIN_PATH


class UnsupportedMeasure(CanonError):
    """Non-additive/semi-additive measure requested (E5, P1)."""

    code = ErrorCode.UNSUPPORTED_MEASURE


class FanoutUnsafe(CanonError):
    """Join would corrupt a non-additive measure (E5, P1)."""

    code = ErrorCode.FANOUT_UNSAFE


class GuardrailBlock(CanonError):
    """A ``severity: error`` guardrail blocked the query; rationale returned (E5/E15)."""

    code = ErrorCode.GUARDRAIL_BLOCK


class ValidationFailed(CanonError):
    """A semantic/contract file failed validation (E5/E15)."""

    code = ErrorCode.VALIDATION_FAILED


class AssertionFailed(CanonError):
    """A benchmark/CI assertion diverged from expected (E5/E15)."""

    code = ErrorCode.ASSERTION_FAILED


class ReadOnlyViolation(CanonError):
    """Raised when a non-read-only SQL statement is submitted to run_read_only_sql()."""

    code = ErrorCode.READ_ONLY_VIOLATION


class SchemaMismatch(CanonError):
    """Raised when declared schema does not match the live source during probe validation."""

    code = ErrorCode.SCHEMA_MISMATCH


class ConnectionError(CanonError):  # noqa: A001 — intentionally shadows builtin in this namespace
    """Raised when a connector cannot establish or maintain a connection."""

    code = ErrorCode.CONNECTION_ERROR


class ContradictionsFound(CanonError):
    """Raised by headless ``--strict`` ingest when a run flags any contradiction (E4 §5.4)."""

    code = ErrorCode.CONTRADICTION


class CredentialError(CanonError):
    """Raised when a credentials_ref cannot be resolved to a secret."""


class SemanticSourceError(CanonError):
    """Raised when a semantics/*.yaml file is invalid; message carries file+line."""


class ContractError(CanonError):
    """Raised when a contracts/*.yaml file is invalid; message carries file+line."""


class KnowledgePageError(CanonError):
    """Raised when a knowledge/**/*.md page is invalid; message carries file+line."""
