"""Contract surface schema — Pydantic models for contracts/**/*.yaml (SPEC-E15 §2.2–2.5)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from canon.semantic.models import Provenance

__all__ = [
    "AppliesTo",
    "Assertion",
    "CanonicalRef",
    "ContractValidationError",
    "DeprecatedAlternative",
    "FinalityRule",
    "Guardrail",
    "GuardrailKind",
    "MetricBinding",
    "Realization",
    "Severity",
    "Status",
]


class ContractValidationError(ValueError):
    """A cross-field validation failure that carries the YAML path it concerns.

    Subclasses ValueError so Pydantic wraps it into a ValidationError on direct
    construction; the loader recovers ``path`` (via the error's ctx) to resolve a
    precise file+line for the message.
    """

    def __init__(self, path: tuple[str | int, ...], message: str) -> None:
        self.path = path
        super().__init__(message)


class Status(StrEnum):
    """Lifecycle status of a contract entity."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"


class Severity(StrEnum):
    """Enforcement severity of a guardrail."""

    ERROR = "error"
    WARN = "warn"


class GuardrailKind(StrEnum):
    """The enforcement mechanism of a guardrail."""

    MANDATORY_FILTER = "mandatory_filter"  # [P0]
    REQUIRED_DIMENSION = "required_dimension"  # [P1]
    RESTRICT_SOURCE = "restrict_source"  # [P1]


class CanonicalRef(BaseModel):
    """The single canonical source+measure for a metric binding."""

    model_config = ConfigDict(frozen=True)

    source: str
    measure: str


class DeprecatedAlternative(BaseModel):
    """A known non-canonical definition, explicitly flagged as superseded."""  # [P1]

    model_config = ConfigDict(frozen=True)

    source: str
    ref: str
    reason: str


class MetricBinding(BaseModel):
    """Canonical metric→source binding (SPEC-E15 §2.2)."""

    model_config = ConfigDict(frozen=True)

    metric: str
    owner: str | None = None
    canonical: CanonicalRef
    provenance: Provenance = Provenance.HUMAN_CURATED
    aliases: list[str] = []
    deprecated_alternatives: list[DeprecatedAlternative] = []
    status: Status = Status.ACTIVE

    @model_validator(mode="after")
    def _validate_aliases(self) -> MetricBinding:
        for i, alias in enumerate(self.aliases):
            if alias == self.metric:
                raise ContractValidationError(
                    ("aliases", i),
                    f"alias {alias!r} duplicates the metric name itself",
                )
        return self


class AppliesTo(BaseModel):
    """Target of a guardrail — either a source(+measure) or a metric name."""

    model_config = ConfigDict(frozen=True)

    source: str | None = None
    measure: str | None = None
    metric: str | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> AppliesTo:
        has_source_shape = self.source is not None
        has_metric_shape = self.metric is not None
        if has_source_shape == has_metric_shape:
            raise ContractValidationError(
                ("applies_to",),
                "applies_to must specify either 'source' or 'metric', not both or neither",
            )
        return self


class Guardrail(BaseModel):
    """An enforceable rule the compiler applies to matching queries (SPEC-E15 §2.3)."""

    model_config = ConfigDict(frozen=True)

    id: str
    applies_to: AppliesTo
    kind: GuardrailKind
    filter: str | None = None
    severity: Severity = Severity.ERROR
    rationale: str
    phase: str | None = None

    @model_validator(mode="after")
    def _validate_filter(self) -> Guardrail:
        if self.kind is GuardrailKind.MANDATORY_FILTER and not self.filter:
            raise ContractValidationError(
                ("filter",),
                "mandatory_filter guardrail requires a non-empty 'filter' expression",
            )
        return self


class Realization(BaseModel):
    """One physical source realization along a finality axis."""  # [P1]

    model_config = ConfigDict(frozen=True)

    source: str
    role: str  # "final" | "provisional"
    watermark: str | None = None
    tz: str | None = None


class FinalityRule(BaseModel):
    """Finality/coalescing rule over a metric's physical realizations (SPEC-E15 §2.4)."""  # [P1]

    model_config = ConfigDict(frozen=True)

    metric: str
    realizations: list[Realization] = []
    coalescing: str | None = None
    result_flag: str | None = None
    board_only_final: bool = False


class Assertion(BaseModel):
    """A trusted query→expected-result check for CI regression (SPEC-E15 §2.5)."""  # [P1]

    model_config = ConfigDict(frozen=True)

    id: str
    query: dict[str, Any]
    expect: dict[str, Any]
    source_of_truth: str | None = None
