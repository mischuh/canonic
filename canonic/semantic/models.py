"""Semantic source schema — the Pydantic model tree for semantics/*.yaml (SPEC-E5 §2.1)."""

from __future__ import annotations

import hashlib
import json
import re
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
    "PackSourceMeta",
    "Provenance",
    "Relationship",
    "SemanticSource",
    "SemanticValidationError",
    "SourceMeta",
    "compute_dimension_fingerprint",
    "compute_measure_fingerprint",
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


#: Characters a ``json_path`` key may not contain. Keys are rendered into dialect-specific
#: path literals and not every sqlglot generator escapes quotes, so they are refused up front.
_JSON_KEY_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f'\"\\]")


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


class PackSourceMeta(BaseModel):
    """Which context pack installed this file (AMENDMENT-context-packs §2.3).

    Stamped once at ``canonic pack add`` time on every emitted semantic source, metric
    binding, guardrail, and knowledge page; ``None`` for anything not pack-installed.
    System-written, human-editable afterward — same status as ``frozen``, not itself
    validated by the compiler. There is no "pack update" that rewrites this later: a
    version bump re-runs install and produces a new reviewable diff like any other change.
    """

    model_config = ConfigDict(frozen=True)

    pack: str
    version: str
    variant: str


class Column(BaseModel):
    """A physical column exposed by the source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: NormalizedType
    nullable: bool = True


class Measure(BaseModel):
    """An aggregation over the source (e.g. sum(amount))."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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


def compute_measure_fingerprint(measure: Measure) -> str:
    """Stable sha256 over a measure's definition, for drift detection (SPEC-E6 §7).

    Mirrors :func:`canonic.connectors.base.compute_fingerprint`'s ``"sha256:<hex>"`` format so
    bound knowledge-page fingerprints read the same as schema fingerprints. Hashes the raw
    ``expr`` literally for v1; whether cosmetic ``expr`` changes should be ignored is an open
    question (SPEC-E6 §12, shared with E15).
    """
    payload = {"expr": measure.expr}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


def compute_dimension_fingerprint(dimension: Dimension) -> str | None:
    """Stable sha256 over a derived dimension's definition, for drift detection (SPEC-E6 §7).

    Covers ``(expr, type, description)`` as AMENDMENT-dimension-expr §5 specifies, plus
    ``(column, json_path)`` for a JSON-path dimension (AMENDMENT-json-path-dimension). A
    plain column dimension has no expression to drift, so it has no fingerprint.
    """
    if not dimension.is_derived:
        return None
    payload: dict[str, Any] = {
        "expr": dimension.expr,
        "type": dimension.type.value if dimension.type else None,
        "description": dimension.description,
    }
    if dimension.json_path is not None:
        # Added only when set, so the fingerprint of an existing ``expr`` dimension is unchanged.
        payload["column"] = dimension.column
        payload["json_path"] = dimension.json_path
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


class Dimension(BaseModel):
    """A column or derived value exposed for grouping/filtering, optionally time-bucketed.

    Three shapes (AMENDMENT-dimension-expr, AMENDMENT-json-path-dimension):

    * ``column``: a physical column as is.
    * ``expr``: a SQL expression over same-source columns.
    * ``column`` + ``json_path``: one key of a JSON column, given as key segments.

    The last two have no column to infer a type from, so ``type`` is required there.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    column: str | None = None
    expr: str | None = None  # [P1] derived dimension, same-source columns only
    json_path: list[str] | None = None  # [P1] key segments into the JSON ``column``
    type: NormalizedType | None = None  # required with ``expr``/``json_path``, else ignored
    granularity: str | None = None  # [P1] time granularity, e.g. "day"
    label: str | None = None  # human-readable display name, e.g. "Product Type"
    description: str | None = None  # freetext explanation
    aliases: list[str] = []  # alternate lookup names, e.g. ["product_type"]

    @model_validator(mode="after")
    def _validate_column_xor_expr(self) -> Dimension:
        if (self.column is None) == (self.expr is None):
            raise ValueError(f"dimension {self.name!r} must set exactly one of 'column' and 'expr'")
        if self.json_path is not None:
            self._validate_json_path()
        if self.is_derived and self.type is None:
            raise ValueError(
                f"dimension {self.name!r} sets 'expr' or 'json_path' and therefore requires 'type'"
            )
        if (
            self.is_derived
            and self.granularity is not None
            and self.type not in {NormalizedType.DATE, NormalizedType.TIMESTAMP}
        ):
            raise ValueError(
                f"dimension {self.name!r} sets 'granularity' on a derived value of type "
                f"{self.type.value if self.type else None!r}; it must be date or timestamp"
            )
        return self

    def _validate_json_path(self) -> None:
        if self.expr is not None:
            raise ValueError(f"dimension {self.name!r} sets 'json_path' and cannot also set 'expr'")
        if not self.json_path:
            raise ValueError(f"dimension {self.name!r} sets an empty 'json_path'")
        for segment in self.json_path:
            if not segment or _JSON_KEY_FORBIDDEN.search(segment):
                raise ValueError(
                    f"dimension {self.name!r} has an invalid 'json_path' key {segment!r}: keys "
                    "must be non-empty and contain no quotes, backslashes or control characters"
                )

    @property
    def is_derived(self) -> bool:
        """Whether the value is computed (``expr`` or ``json_path``) rather than a bare column."""
        return self.expr is not None or self.json_path is not None

    def backing_columns(self) -> set[str]:
        """Physical column names this dimension reads (the one column, or the expr's columns)."""
        if self.column is not None:
            return {self.column}
        assert self.expr is not None  # noqa: S101 — guaranteed by the validator above
        return _columns_in_expr(self.expr)

    def value_type(self, columns: list[Column]) -> NormalizedType | None:
        """The dimension's type: declared for a derived value, else the backing column's."""
        if self.is_derived:
            return self.type
        return next((c.type for c in columns if c.name == self.column), None)


class Join(BaseModel):
    """A declared join path to another semantic source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    to: str
    on: str
    relationship: Relationship
    name: str | None = None  # SQL alias for the target table; defaults to ``to``

    @property
    def alias(self) -> str:
        """SQL alias used when this join's target table appears in a query."""
        return self.name if self.name else self.to


class Filter(BaseModel):
    """A named reusable predicate."""  # [P1]

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    expr: str


class FinalityMeta(BaseModel):
    """Finality watermark for provisional/final result tagging."""  # [P1]

    model_config = ConfigDict(frozen=True, extra="forbid")

    watermark: str | None = None  # null = always-final source


class SourceMeta(BaseModel):
    """System-managed provenance metadata.

    Written only by ``canonic ingest`` (sets ``inferred``) or ``canonic review``/``apply``
    via ``curate``/``freeze`` (AMENDMENT-provenance-promotion) — never edited directly.
    """

    model_config = ConfigDict(frozen=True)

    provenance: Provenance = Provenance.INFERRED
    source_fingerprint: str | None = None  # sha256 of the introspected/declared schema
    # sha256 of the modeling definitions (dbt, Ossie) folded into the draft. Compared only
    # when both the accepted file and the proposal carry one.
    definition_fingerprint: str | None = None
    last_validated_at: datetime | None = None
    frozen: bool = False
    pack_source: PackSourceMeta | None = None


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

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str  # [P0] unique across the whole project (enforced by list_semantic_sources)
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
        self._reject_duplicates("joins", "join alias", [j.alias for j in self.joins])

        # Grain columns must be declared.
        for i, g in enumerate(self.grain):
            if g not in column_names:
                raise SemanticValidationError(
                    ("grain", i), f"grain column {g!r} is not a declared column"
                )

        # Dimension column and expr references must be declared on this source.
        for i, dim in enumerate(self.dimensions):
            if dim.column is not None:
                if dim.column not in column_names:
                    raise SemanticValidationError(
                        ("dimensions", i, "column"),
                        f"dimension {dim.name!r} references undeclared column {dim.column!r}",
                    )
                column_type = next(c.type for c in self.columns if c.name == dim.column)
                if dim.json_path is not None and column_type is not NormalizedType.JSON:
                    raise SemanticValidationError(
                        ("dimensions", i, "json_path"),
                        f"dimension {dim.name!r} sets 'json_path' on column {dim.column!r} "
                        f"of type {column_type.value!r}; it must be a json column",
                    )
                continue
            try:
                refs = dim.backing_columns()
            except ValueError as exc:
                raise SemanticValidationError(("dimensions", i, "expr"), str(exc)) from exc
            for ref in sorted(refs):
                if ref not in column_names:
                    raise SemanticValidationError(
                        ("dimensions", i, "expr"),
                        f"dimension {dim.name!r} references undeclared column {ref!r}",
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
