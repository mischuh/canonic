"""Contract surface schema — Pydantic models for contracts/**/*.yaml (SPEC-E15 §2.2–2.5)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from canon.semantic.models import Provenance

__all__ = [
    "AppliesTo",
    "Assertion",
    "AssertionExpect",
    "BindingKind",
    "CanonicalRef",
    "CollapseAgg",
    "ContractValidationError",
    "DeprecatedAlternative",
    "FinalityRule",
    "Guardrail",
    "GuardrailKind",
    "MetricBinding",
    "OnZeroDenominator",
    "Realization",
    "RestrictTo",
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


class BindingKind(StrEnum):
    """Compilation strategy for a metric binding (SPEC-Fuller-E15 §3)."""

    SINGLE = "single"
    RATIO = "ratio"
    WEIGHTED_AVG = "weighted_avg"
    SEMI_ADDITIVE = "semi_additive"
    DISTINCT_COUNT = "distinct_count"
    PERCENTILE = "percentile"
    OPAQUE = "opaque"


class CollapseAgg(StrEnum):
    """How to collapse the non-additive dimension in a semi_additive binding (§4.2)."""

    LAST = "last"
    FIRST = "first"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class OnZeroDenominator(StrEnum):
    """Behaviour when the denominator of a composable_post_agg metric is zero (§4.1)."""

    NULL = "null"
    ZERO = "zero"
    ERROR = "error"


class CanonicalRef(BaseModel):
    """The canonical binding for a metric — either a single source+measure or a composite (§3).

    For ``kind=single`` (default), ``source`` and ``measure`` are required.
    For ``kind=ratio``, ``numerator`` and ``denominator`` (metric names) are required.
    For ``kind=weighted_avg``, ``weighted_sum`` and ``weight`` (metric names) are required.
    For ``kind=semi_additive``, ``source``, ``measure``, ``collapse_dimension``, and
    ``collapse_agg`` are required.
    For ``kind=distinct_count``, ``source`` and ``distinct_on`` (column name) are required.
    For ``kind=percentile``, ``source``, ``column``, and ``quantile`` ∈ (0, 1) are required.
    For ``kind=opaque``, ``source``, ``measure``, and ``native_grain`` (non-empty list of
    dimension column names) are required. Served only at its declared native grain; any
    other grain returns UNSUPPORTED_MEASURE (§4.4).

    ``population_filter`` is an optional SQL predicate (§4.5) valid for every ``kind``. It defines
    the population the metric is *about* and is AND-ed into the WHERE of every leaf query before
    aggregation and before per-leaf guardrails.
    """

    model_config = ConfigDict(frozen=True)

    kind: BindingKind = BindingKind.SINGLE
    source: str | None = None
    measure: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    weighted_sum: str | None = None
    weight: str | None = None
    on_zero_denominator: OnZeroDenominator = OnZeroDenominator.NULL
    collapse_dimension: str | None = None
    collapse_agg: CollapseAgg | None = None
    distinct_on: str | None = None
    column: str | None = None
    quantile: float | None = None
    native_grain: list[str] | None = None
    population_filter: str | None = None

    @field_validator("on_zero_denominator", mode="before")
    @classmethod
    def _coerce_on_zero(cls, v: object) -> object:
        if v is None:
            return OnZeroDenominator.NULL
        return v

    @model_validator(mode="after")
    def _validate_shape(self) -> CanonicalRef:
        if self.kind is BindingKind.SINGLE:
            if self.source is None:
                raise ContractValidationError(("source",), "single binding requires 'source'")
            if self.measure is None:
                raise ContractValidationError(("measure",), "single binding requires 'measure'")
        elif self.kind is BindingKind.RATIO:
            if self.numerator is None:
                raise ContractValidationError(("numerator",), "ratio binding requires 'numerator'")
            if self.denominator is None:
                raise ContractValidationError(
                    ("denominator",), "ratio binding requires 'denominator'"
                )
        elif self.kind is BindingKind.WEIGHTED_AVG:
            if self.weighted_sum is None:
                raise ContractValidationError(
                    ("weighted_sum",), "weighted_avg binding requires 'weighted_sum'"
                )
            if self.weight is None:
                raise ContractValidationError(("weight",), "weighted_avg binding requires 'weight'")
        elif self.kind is BindingKind.SEMI_ADDITIVE:
            if self.source is None:
                raise ContractValidationError(
                    ("source",), "semi_additive binding requires 'source'"
                )
            if self.measure is None:
                raise ContractValidationError(
                    ("measure",), "semi_additive binding requires 'measure'"
                )
            if self.collapse_dimension is None:
                raise ContractValidationError(
                    ("collapse_dimension",),
                    "semi_additive binding requires 'collapse_dimension'",
                )
            if self.collapse_agg is None:
                raise ContractValidationError(
                    ("collapse_agg",), "semi_additive binding requires 'collapse_agg'"
                )
        elif self.kind is BindingKind.DISTINCT_COUNT:
            if self.source is None:
                raise ContractValidationError(
                    ("source",), "distinct_count binding requires 'source'"
                )
            if self.distinct_on is None:
                raise ContractValidationError(
                    ("distinct_on",), "distinct_count binding requires 'distinct_on'"
                )
        elif self.kind is BindingKind.PERCENTILE:
            if self.source is None:
                raise ContractValidationError(("source",), "percentile binding requires 'source'")
            if self.column is None:
                raise ContractValidationError(("column",), "percentile binding requires 'column'")
            if self.quantile is None or not (0 < self.quantile < 1):
                raise ContractValidationError(
                    ("quantile",), "percentile binding requires quantile ∈ (0, 1)"
                )
        elif self.kind is BindingKind.OPAQUE:
            if self.source is None:
                raise ContractValidationError(("source",), "opaque binding requires 'source'")
            if self.measure is None:
                raise ContractValidationError(("measure",), "opaque binding requires 'measure'")
            if not self.native_grain:
                raise ContractValidationError(
                    ("native_grain",), "opaque binding requires non-empty 'native_grain'"
                )
        return self


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


class RestrictTo(BaseModel):
    """Target role for a restrict_source guardrail (SPEC-E15 §2.4)."""

    model_config = ConfigDict(frozen=True)

    role: str  # "final" | "provisional"

    @model_validator(mode="after")
    def _validate_role(self) -> RestrictTo:
        if self.role not in {"final", "provisional"}:
            raise ContractValidationError(
                ("role",),
                f"restrict_to.role must be 'final' or 'provisional', got {self.role!r}",
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
    restrict_to: RestrictTo | None = None
    context: str | None = None
    severity: Severity = Severity.ERROR
    rationale: str
    phase: str | None = None

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> Guardrail:
        if self.kind is GuardrailKind.MANDATORY_FILTER and not self.filter:
            raise ContractValidationError(
                ("filter",),
                "mandatory_filter guardrail requires a non-empty 'filter' expression",
            )
        if self.kind is GuardrailKind.RESTRICT_SOURCE:
            if self.restrict_to is None:
                raise ContractValidationError(
                    ("restrict_to",),
                    "restrict_source guardrail requires a 'restrict_to' field",
                )
            if not self.context:
                raise ContractValidationError(
                    ("context",),
                    "restrict_source guardrail requires a non-empty 'context' field",
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

    @model_validator(mode="after")
    def _validate_structure(self) -> FinalityRule:
        from canon.contracts.finality import validate_finality_rule

        try:
            validate_finality_rule(self)
        except ValueError as exc:
            raise ContractValidationError(("realizations",), str(exc)) from exc
        return self


class AssertionExpect(BaseModel):
    """The expected result of an assertion — a scalar/row-set check (SPEC-Fuller-E15 §3.1).

    ``rows`` (optional) pins the expected row count. ``values`` maps an output column
    name to its expected value (compared with ``tolerance`` when numeric). ``tolerance``
    is a *relative* tolerance (e.g. ``0.01`` = 1%); ``None`` means exact match.
    """

    model_config = ConfigDict(frozen=True)

    rows: int | None = None
    values: dict[str, Any] = {}
    tolerance: float | None = None


class Assertion(BaseModel):
    """A trusted query→expected-result check for CI regression (SPEC-E15 §2.5)."""  # [P1]

    model_config = ConfigDict(frozen=True)

    id: str
    query: dict[str, Any]
    expect: AssertionExpect = AssertionExpect()
    source_of_truth: str | None = None
