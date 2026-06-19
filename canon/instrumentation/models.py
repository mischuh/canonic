"""AnswerEvent model for SPEC-E16 §3 — the served-answer record shape."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

__all__ = ["AnswerEvent"]


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
