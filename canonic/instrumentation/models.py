"""AnswerEvent, ReconcileDecisionEvent, and FunnelEvent models for SPEC-E16 §3 / §11 S4/S6."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "AnswerEvent",
    "AnswerOutcomeEvent",
    "FunnelEvent",
    "FunnelMilestone",
    "OutcomeMarkedBy",
    "OutcomeReasonCode",
    "OutcomeVerdict",
    "ReconcileDecisionEvent",
]


def _sha256_json(payload: Any) -> str:
    """Return ``sha256:<hex>`` for a JSON-serialisable payload (stable key order)."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


def _age_days(last_validated_at: str | None) -> int | None:
    """Days elapsed since ``last_validated_at`` (ISO string) relative to now(UTC)."""
    if last_validated_at is None:
        return None
    try:
        validated = datetime.fromisoformat(last_validated_at)
        if validated.tzinfo is None:
            validated = validated.replace(tzinfo=UTC)
        return (datetime.now(UTC) - validated).days
    except ValueError:
        return None


class AnswerEvent(BaseModel):
    """One served-answer record appended to ``.canonic/events.jsonl`` (SPEC-E16 §3).

    ``trust_score`` carries the E14 trust tier (``caution``/``provisional``/``trusted``)
    populated by the serving path (SPEC-E16 Part 2 §4). ``cache_hit``/``over_limit_blocked``
    remain reserved as ``null`` until E13 lands (S3-AC1).
    """

    model_config = ConfigDict(frozen=True)

    ts: str
    kind: Literal["served_answer"] = "served_answer"
    contract_schema: str
    query_hash: str
    compiled_sql_hash: str | None
    connection: str | None
    resolved: dict[str, dict[str, str]] = {}
    guardrails_fired: list[str] = []
    finality: dict[str, int] | None = None
    freshness: list[dict[str, Any]] = []
    latency_ms: int
    bytes_scanned: int | None = None
    error: str | None = None
    trust_score: str | None = None
    # Verified caller identity (bearer-token client_id) for MCP http-transport calls;
    # None for stdio transport and the CLI, which have no auth layer
    # (AMENDMENT-remote-mcp-transport.md).
    user: str | None = None
    # reserved — null until E13 lands (S3-AC1):
    cache_hit: bool | None = None
    over_limit_blocked: bool | None = None


class ReconcileDecisionEvent(BaseModel):
    """One reconcile-decision record appended to ``.canonic/events.jsonl`` (SPEC-E16 §11 S4).

    Field set owned by E4 §6; E16 owns the substrate (the shared file and writer).
    ``ts`` unifies the timestamp key across both event kinds for §7 metric joins.
    ``run_id`` cross-references the ``.canonic/pending-diffs/<run-id>/`` directory written
    by the same ingest run (GH-149, AC4).
    """

    model_config = ConfigDict(frozen=True)

    ts: str
    kind: Literal["reconcile_decision"] = "reconcile_decision"
    run_id: str
    decision: str
    target: str
    op: str
    provenance: str
    confidence: float
    anchored_to: list[str] = []
    drafted_by: str
    auto_apply: bool = False
    low_confidence: bool = False
    existing_frozen: bool = False


class OutcomeVerdict(StrEnum):
    """Whether a served answer was correct (SPEC-E16 Part 2 §3)."""

    CORRECT = "correct"
    INCORRECT = "incorrect"


class OutcomeReasonCode(StrEnum):
    """What was wrong, when ``verdict`` is ``incorrect`` (SPEC-E16 Part 2 §3).

    This is the attribution safeguard: acting on the wrong cause corrupts good context.
    Only ``WRONG_DEFINITION`` implicates the binding used and becomes contradiction
    evidence for E4 (via E11); ``WRONG_DATA`` and ``WRONG_INTERPRETATION`` are recorded
    but do not flag the binding; ``UNSPECIFIED`` carries lower weight and alone flags
    nothing. E16 records the code faithfully — E11 decides what may act on it.
    """

    WRONG_DEFINITION = "wrong_definition"
    WRONG_DATA = "wrong_data"
    WRONG_INTERPRETATION = "wrong_interpretation"
    UNSPECIFIED = "unspecified"


class OutcomeMarkedBy(StrEnum):
    """Who produced the outcome verdict (SPEC-E16 Part 2 §3), retained so E11 can weight sources."""

    ANALYST = "analyst"
    AGENT = "agent"
    CI = "ci"


class AnswerOutcomeEvent(BaseModel):
    """The FR-9 ground-truth feed: a correct/incorrect mark on a served answer (SPEC-E16 Part 2 §3).

    ``ref`` links back to the originating ``AnswerEvent.query_hash``. An ``incorrect``
    verdict without an explicit ``reason_code`` defaults to ``unspecified`` (SPEC-E16 Part 2
    §9 — conservative default so E11 can weight it low rather than guessing the cause).
    """

    model_config = ConfigDict(frozen=True)

    ts: str
    kind: Literal["answer_outcome"] = "answer_outcome"
    ref: str
    verdict: OutcomeVerdict
    reason_code: OutcomeReasonCode | None = None
    correction: str | None = None
    marked_by: OutcomeMarkedBy

    @model_validator(mode="before")
    @classmethod
    def _default_reason_code(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and data.get("verdict") == OutcomeVerdict.INCORRECT.value
            and data.get("reason_code") is None
        ):
            return {**data, "reason_code": OutcomeReasonCode.UNSPECIFIED.value}
        return data

    @model_validator(mode="after")
    def _validate_reason_code(self) -> AnswerOutcomeEvent:
        if self.verdict is OutcomeVerdict.CORRECT and self.reason_code is not None:
            raise ValueError("reason_code must be omitted when verdict is 'correct'")
        return self


class FunnelMilestone(StrEnum):
    """Onboarding funnel milestones emitted to the E16 event log (SPEC-onboarding §9, OB-S6)."""

    SETUP_STARTED = "setup_started"
    CONNECTION_ADDED = "connection_added"
    BOOTSTRAP_COMPLETED = "bootstrap_completed"
    FIRST_ANSWER_SERVED = "first_answer_served"
    FIRST_CURATED_REVIEW_COMPLETED = "first_curated_review_completed"


class FunnelEvent(BaseModel):
    """Content-free onboarding funnel milestone appended to ``.canonic/events.jsonl`` (OB-S6).

    No warehouse content — only the milestone name and timestamp.
    """

    model_config = ConfigDict(frozen=True)

    ts: str
    kind: Literal["funnel_milestone"] = "funnel_milestone"
    milestone: FunnelMilestone
