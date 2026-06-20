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
    # LLM runtime errors (SPEC-E10 §8); structured so E4 callers act programmatically.
    GENERATION_FAILED = "generation_failed"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"
    STRUCTURED_OUTPUT_UNSUPPORTED = "structured_output_unsupported"
    # Transient provider/transport failure that persisted past the bounded retry budget.
    # Distinct from GENERATION_FAILED (deterministic one-shot provider rejection).
    RETRIES_EXHAUSTED = "retries_exhausted"
    # Air-gapped egress guard (SPEC-E10 §4); raised at config load and before any model
    # call when air_gapped mode would allow context to leave the machine/network.
    AIR_GAPPED_VIOLATION = "air_gapped_violation"


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
    ErrorCode.GENERATION_FAILED: 15,
    ErrorCode.STRUCTURED_OUTPUT_INVALID: 16,
    ErrorCode.STRUCTURED_OUTPUT_UNSUPPORTED: 17,
    ErrorCode.AIR_GAPPED_VIOLATION: 18,
    ErrorCode.RETRIES_EXHAUSTED: 19,
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


class CapabilityNotSupportedError(CanonError):
    """Raised when a connector is asked to honor a Capability it does not declare.

    The hard E3 no-execution boundary (SPEC-E3 §2, S8): definition/evidence connectors
    never expose ``run_read_only_sql`` or ``introspect_schema``. Asking one to execute SQL
    or introspect is a caller contract slip — surfaced as this typed error (default exit 1)
    rather than a bare ``AttributeError``. Carries no wire ``ErrorCode`` for the same reason
    as :class:`EmbeddingUnavailable`.
    """


class ConnectionError(CanonError):  # noqa: A001 — intentionally shadows builtin in this namespace
    """Raised when a connector cannot establish or maintain a connection."""

    code = ErrorCode.CONNECTION_ERROR


class UnsupportedSourceVersionError(ConnectionError):
    """A connector's source version falls outside its pinned supported range (SPEC-E3 §6, S5).

    Subclasses :class:`ConnectionError` (exit 13) so existing connection-error handling still
    catches it, while callers that care can catch this specifically and read the structured
    ``detected`` / ``supported`` fields. Raised at ``test_connection``/extract time before any
    data lands, so an out-of-range source ingests nothing — no silent partial ingest.
    """

    def __init__(self, source: str, *, detected: str, supported: str) -> None:
        self.source = source
        self.detected = detected
        self.supported = supported
        super().__init__(f"{source} version {detected!r} is unsupported; supported: {supported}")


class UnknownConnectorType(ConnectionError):
    """A connection declares a ``type`` with no connector registered in the factory (E2 S9, GH-102).

    Subclasses :class:`ConnectionError` (exit 13) so existing connection-error handling still
    catches it, while callers that care can catch this specifically. The message lists the
    registered types — no silent fallback (AC2).
    """

    def __init__(self, type_name: str, *, known: Sequence[str]) -> None:
        self.type_name = type_name
        self.known = tuple(known)
        listed = ", ".join(self.known) or "(none)"
        super().__init__(f"unknown connector type {type_name!r}; registered types: {listed}")


class ContradictionsFound(CanonError):
    """Raised by headless ``--strict`` ingest when a run flags any contradiction (E4 §5.4)."""

    code = ErrorCode.CONTRADICTION


class GenerationError(CanonError):
    """A generation call failed (provider error, transport, or non-OpenAI-compatible provider).

    The catch-all for the E10 generation runtime (SPEC-E10 §8). No silent model
    substitution: a failed call surfaces this structured error rather than quietly
    falling back to a different model.
    """

    code = ErrorCode.GENERATION_FAILED


class StructuredOutputError(CanonError):
    """The model returned output that does not satisfy the requested JSON schema (SPEC-E10 §2).

    Distinct from :class:`StructuredOutputUnsupported`: here the endpoint accepted the
    schema-constrained request but produced output that fails validation.
    """

    code = ErrorCode.STRUCTURED_OUTPUT_INVALID


class StructuredOutputUnsupported(CanonError):
    """The endpoint/model cannot honor JSON-schema-constrained output at all (SPEC-E10 §2).

    Raised when the backend rejects a structured-output request outright, so the caller
    gets a clear error instead of unparseable prose to scrape (baseline caveat, §7).
    """

    code = ErrorCode.STRUCTURED_OUTPUT_UNSUPPORTED


class RetriesExhausted(CanonError):
    """A transient provider/transport failure persisted past the bounded retry budget (SPEC-E10 §3).

    Distinct from :class:`GenerationError`: here the endpoint was reachable but produced
    transient failures on every attempt up to the configured limit. The error carries the
    attempt count in its message. Callers that want to distinguish a timeout-after-retries
    scenario from a deterministic provider rejection should catch this separately.
    """

    code = ErrorCode.RETRIES_EXHAUSTED


class AirGappedViolation(CanonError):
    """Raised when air-gapped mode would let warehouse content or context leave the machine.

    The enforced privacy guarantee of SPEC-E10 §4: under ``runtime.air_gapped: true`` every
    model endpoint must resolve to a local/allowlisted address. Raised at config load when a
    public endpoint, enabled telemetry, or a remote secret-service ``*_ref`` is configured,
    and at call time before any request leaves the process. Defense-in-depth, like DB
    read-only (E2 §3) — not advisory.
    """

    code = ErrorCode.AIR_GAPPED_VIOLATION


class EmbeddingUnavailable(CanonError):
    """Raised when the embedding runtime is asked to embed while unavailable (SPEC-E10 §5).

    The local embedding backend (``sentence-transformers``) is an optional add-on. When it
    is not installed or its model failed to load, :meth:`EmbeddingRuntime.is_available`
    reports false and E6 degrades to lexical-only — never an error. This is raised only when
    a caller invokes ``embed`` without first gating on ``is_available()``; it carries no wire
    ``ErrorCode`` (default exit ``1``) since it signals a caller contract slip, not a
    user-facing failure mode.
    """


class CredentialError(CanonError):
    """Raised when a credentials_ref cannot be resolved to a secret."""


class EvalDatasetError(CanonError):
    """Raised when a baseline dataset, candidates file, or task arg is invalid (SPEC-E10 §7, GH-66).

    Operator-input error for the ``canon eval baseline`` harness: a labeled ``draft`` case that is
    not valid JSON / fails schema validation, a candidates file that does not parse into
    ``LLMConfig`` entries, or an unsupported ``--task``. Carries the file and line/entry in its
    message. Reuses ``VALIDATION_FAILED`` since it is the same class of failure as a malformed
    semantic/contract file — bad input to a command, surfaced structurally.
    """

    code = ErrorCode.VALIDATION_FAILED


class SemanticSourceError(CanonError):
    """Raised when a semantics/*.yaml file is invalid; message carries file+line."""


class ContractError(CanonError):
    """Raised when a contracts/*.yaml file is invalid; message carries file+line."""


class KnowledgePageError(CanonError):
    """Raised when a knowledge/**/*.md page is invalid; message carries file+line."""


class KnowledgeReferenceError(CanonError):
    """A knowledge page references a target that does not resolve; blocks the write.

    Raised at the write boundary (SPEC-E6 §3.1) when an ``sl_ref``, page ``ref``, or body
    ``[[wikilink]]`` points at nothing. Carries the broken ``ref``, its ``kind``
    (``sl_ref``/``ref``/``wikilink``), and the frontmatter ``path`` it sits at; ``message``
    embeds the page file plus a precise location.
    """

    code = ErrorCode.VALIDATION_FAILED

    def __init__(self, path: tuple[str | int, ...], ref: str, kind: str, message: str) -> None:
        self.path = path
        self.ref = ref
        self.kind = kind
        super().__init__(message)
