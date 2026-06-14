"""Semantic source schema — the Pydantic model tree for semantics/*.yaml (SPEC-E5 §2.1)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime
from enum import StrEnum
from typing import Any

import sqlglot
from pydantic import BaseModel, ConfigDict, model_validator
from sqlglot import exp

__all__ = [
    "Additivity",
    "Column",
    "Dimension",
    "Filter",
    "FinalityMeta",
    "Join",
    "Measure",
    "NormalizedType",
    "Provenance",
    "Relationship",
    "SemanticSource",
    "SemanticValidationError",
    "SourceMeta",
]


class SemanticValidationError(ValueError):
    """A cross-field validation failure that carries the YAML path it concerns.

    Subclasses ValueError so Pydantic wraps it into a ValidationError on direct
    construction; the loader recovers ``path`` (via the error's ctx) to resolve a
    precise file+line for the message.
    """

    def __init__(self, path: tuple[str | int, ...], message: str) -> None:
        self.path = path
        super().__init__(message)


# Aggregate functions a P0 measure may use and still be compilable. Measures
# outside this set (or non-additive) are valid in YAML but flagged
# UNSUPPORTED_MEASURE by the compiler (SPEC-E5 §4 step 4), never at load time.
_P0_AGG_FUNCTIONS: frozenset[type[exp.AggFunc]] = frozenset({exp.Sum, exp.Count, exp.Min, exp.Max})


class NormalizedType(StrEnum):
    """The dialect-neutral internal type set (SPEC-E5 §2.1 "Typing")."""

    STRING = "string"
    INT = "int"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOL = "bool"
    DATE = "date"
    TIMESTAMP = "timestamp"
    JSON = "json"


class Additivity(StrEnum):
    """How a measure aggregates across dimensions."""

    ADDITIVE = "additive"  # [P0]
    SEMI_ADDITIVE = "semi_additive"  # [P1]
    NON_ADDITIVE = "non_additive"  # [P1]


class Relationship(StrEnum):
    """Cardinality of a join between two semantic sources."""

    ONE_TO_ONE = "one_to_one"
    MANY_TO_ONE = "many_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_MANY = "many_to_many"


class Provenance(StrEnum):
    """Trust origin of a semantic source's schema (system-managed)."""

    BOARD_APPROVED = "board_approved"
    HUMAN_CURATED = "human_curated"
    INFERRED = "inferred"


class Column(BaseModel):
    """A physical column exposed by the source."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: NormalizedType
    nullable: bool = True


class Measure(BaseModel):
    """An aggregation over the source (e.g. sum(amount))."""

    model_config = ConfigDict(frozen=True)

    name: str
    expr: str
    additivity: Additivity = Additivity.ADDITIVE
    # [P1] dims over which a semi-additive measure is NOT additive
    semi_additive_over: list[str] = []

    @property
    def is_p0_compilable(self) -> bool:
        """True if this measure is additive and uses only a P0 aggregate function.

        Non-compilable measures are accepted at load but rejected by the compiler
        with UNSUPPORTED_MEASURE (SPEC-E5 §4 step 4).
        """
        if self.additivity is not Additivity.ADDITIVE:
            return False
        try:
            parsed = sqlglot.parse_one(self.expr)
        except Exception:  # noqa: BLE001 — any parse failure means not compilable
            return False
        agg = parsed if isinstance(parsed, exp.AggFunc) else parsed.find(exp.AggFunc)
        if agg is None:
            return False
        if isinstance(agg, exp.Count) and agg.args.get("this") and agg.this.find(exp.Distinct):
            return False  # count(distinct …) does not sum across fanout
        return type(agg) in _P0_AGG_FUNCTIONS


class Dimension(BaseModel):
    """A column exposed for grouping/filtering, optionally time-bucketed."""

    model_config = ConfigDict(frozen=True)

    name: str
    column: str
    granularity: str | None = None  # [P1] time granularity, e.g. "day"


class Join(BaseModel):
    """A declared join path to another semantic source."""

    model_config = ConfigDict(frozen=True)

    to: str
    on: str
    relationship: Relationship


class Filter(BaseModel):
    """A named reusable predicate."""  # [P1]

    model_config = ConfigDict(frozen=True)

    name: str
    expr: str


class FinalityMeta(BaseModel):
    """Finality watermark for provisional/final result tagging."""  # [P1]

    model_config = ConfigDict(frozen=True)

    watermark: str | None = None  # null = always-final source


class SourceMeta(BaseModel):
    """System-managed provenance metadata, not hand-edited."""

    model_config = ConfigDict(frozen=True)

    provenance: Provenance = Provenance.INFERRED
    source_fingerprint: str | None = None  # sha256 of the introspected/declared schema
    last_validated_at: datetime | None = None


def _columns_in_expr(expr: str) -> set[str]:
    """Parse a SQL expression and return the set of referenced column names.

    Raises ValueError if the expression cannot be parsed.
    """
    try:
        parsed = sqlglot.parse_one(expr)
    except Exception as exc:  # noqa: BLE001 — surface any sqlglot failure as a validation error
        raise ValueError(f"cannot parse expression {expr!r}: {exc}") from exc
    if parsed is None:
        raise ValueError(f"empty expression {expr!r}")
    return {col.name for col in parsed.find_all(exp.Column)}


class SemanticSource(BaseModel):
    """One queryable relation described for agent reasoning (SPEC-E5 §2.1)."""

    model_config = ConfigDict(frozen=True)

    name: str  # [P0] unique within connection
    connection: str  # [P0]
    table: str  # [P0] physical relation
    grain: list[str]  # [P0] row uniqueness; drives fanout safety
    columns: list[Column]  # [P0]
    measures: list[Measure] = []  # [P0]
    dimensions: list[Dimension] = []  # [P0]
    joins: list[Join] = []  # [P0]
    filters: list[Filter] = []  # [P1]
    segments: list[Any] = []  # [L] named row subsets
    finality: FinalityMeta = FinalityMeta()  # [P1]
    meta: SourceMeta = SourceMeta()  # [P0] system-managed
    description: str | None = None  # [P1]

    @model_validator(mode="after")
    def _validate_references(self) -> SemanticSource:
        """Enforce the write-time semantic-source rules (SPEC-E5 §7)."""
        column_names = {c.name for c in self.columns}

        self._reject_duplicates("columns", "column", [c.name for c in self.columns])
        self._reject_duplicates("measures", "measure", [m.name for m in self.measures])
        self._reject_duplicates("dimensions", "dimension", [d.name for d in self.dimensions])

        # Grain columns must be declared.
        for i, g in enumerate(self.grain):
            if g not in column_names:
                raise SemanticValidationError(
                    ("grain", i), f"grain column {g!r} is not a declared column"
                )

        # Dimension column references must be declared.
        for i, dim in enumerate(self.dimensions):
            if dim.column not in column_names:
                raise SemanticValidationError(
                    ("dimensions", i, "column"),
                    f"dimension {dim.name!r} references undeclared column {dim.column!r}",
                )

        # Measure expressions may reference only declared columns.
        for i, measure in enumerate(self.measures):
            try:
                refs = _columns_in_expr(measure.expr)
            except ValueError as exc:
                raise SemanticValidationError(("measures", i, "expr"), str(exc)) from exc
            for ref in sorted(refs):
                if ref not in column_names:
                    raise SemanticValidationError(
                        ("measures", i, "expr"),
                        f"measure {measure.name!r} references undeclared column {ref!r}",
                    )

        return self

    @staticmethod
    def _reject_duplicates(yaml_key: str, kind: str, names: list[str]) -> None:
        seen: set[str] = set()
        for i, n in enumerate(names):
            if n in seen:
                raise SemanticValidationError((yaml_key, i), f"duplicate {kind} name {n!r}")
            seen.add(n)
