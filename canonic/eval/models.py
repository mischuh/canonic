"""Result models for the baseline harness (SPEC-E10 §7, GH-66)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from canonic.runtime.resolver import Task  # noqa: TC001 — Pydantic resolves annotations at runtime

__all__ = [
    "BaselineReport",
    "CaseOutcome",
    "ModelTaskSummary",
    "StructuredOutcome",
]


class StructuredOutcome(StrEnum):
    """How a model honored the JSON-schema-constrained request for one case (SPEC-E10 §2, §7).

    The four states map one-to-one to the E10 generation outcomes the harness observes, so the
    baseline can report *structured-output behavior* — the axis on which smaller local models
    vary most.
    """

    #: Returned schema-valid JSON the drafter parsed.
    HONORED = "honored"
    #: Endpoint accepted the schema but the model's output failed validation (StructuredOutputError).
    SCHEMA_INVALID = "schema_invalid"
    #: Endpoint cannot honor schema-constrained output at all (StructuredOutputUnsupported).
    UNSUPPORTED = "unsupported"
    #: Provider/transport failure past retries, or an unresolved credential (Generation/Credential).
    ERROR = "error"


class CaseOutcome(BaseModel):
    """One (candidate model, labeled case) run."""

    model_config = ConfigDict(frozen=True)

    relation: str
    correct: bool
    structured: StructuredOutcome
    latency_ms: float
    total_tokens: int | None = None
    predicted_grain: list[str] = []
    expected_grain: list[str] = []
    error: str | None = None


class ModelTaskSummary(BaseModel):
    """Aggregate of one candidate over the whole labeled set for a task."""

    model_config = ConfigDict(frozen=True)

    name: str  # operator-facing candidate label
    model: str  # the resolved model id
    task: Task
    n: int
    accuracy: float  # share of cases whose grain matched exactly
    schema_adherence: float  # share of cases that were HONORED
    p50_latency_ms: float
    median_total_tokens: int | None = None
    outcome_counts: dict[StructuredOutcome, int] = {}
    outcomes: list[CaseOutcome] = []


class BaselineReport(BaseModel):
    """The full per-task baseline: every candidate's summary plus the recommended pairing."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    task: Task
    adherence_floor: float
    summaries: list[ModelTaskSummary] = []
    #: Candidate ``name`` recommended for this task — highest accuracy among those clearing the
    #: structured-output floor. ``None`` when no candidate clears it.
    recommended: str | None = None
