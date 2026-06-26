"""AnswerEvent, ReconcileDecisionEvent, and FunnelEvent models for SPEC-E16 §3 / §11 S4/S6."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

__all__ = ["AnswerEvent", "FunnelEvent", "FunnelMilestone", "ReconcileDecisionEvent"]


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
    """One served-answer record appended to ``.canon/events.jsonl`` (SPEC-E16 §3).

    Reserved fields (``trust_score``, ``cache_hit``, ``over_limit_blocked``) are
    present in the v1 shape as ``null`` so E13/E14 can populate them later without
    a schema migration (S3-AC1).
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
    # reserved — null until E13/E14 land (S3-AC1):
    trust_score: float | None = None
    cache_hit: bool | None = None
    over_limit_blocked: bool | None = None


class ReconcileDecisionEvent(BaseModel):
    """One reconcile-decision record appended to ``.canon/events.jsonl`` (SPEC-E16 §11 S4).

    Field set owned by E4 §6; E16 owns the substrate (the shared file and writer).
    ``ts`` unifies the timestamp key across both event kinds for §7 metric joins.
    """

    model_config = ConfigDict(frozen=True)

    ts: str
    kind: Literal["reconcile_decision"] = "reconcile_decision"
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


class FunnelMilestone(StrEnum):
    """Onboarding funnel milestones emitted to the E16 event log (SPEC-onboarding §9, OB-S6)."""

    SETUP_STARTED = "setup_started"
    CONNECTION_ADDED = "connection_added"
    BOOTSTRAP_COMPLETED = "bootstrap_completed"
    FIRST_ANSWER_SERVED = "first_answer_served"
    FIRST_CURATED_REVIEW_COMPLETED = "first_curated_review_completed"


class FunnelEvent(BaseModel):
    """Content-free onboarding funnel milestone appended to ``.canon/events.jsonl`` (OB-S6).

    No warehouse content — only the milestone name and timestamp.
    """

    model_config = ConfigDict(frozen=True)

    ts: str
    kind: Literal["funnel_milestone"] = "funnel_milestone"
    milestone: FunnelMilestone
