"""Modeling-tier definitions folded into relation drafts.

Definition connectors (dbt, Ossie) describe tables that another, queryable connection
introspects. The builder drafts one ``semantics/<connection>/<name>.yaml`` per introspected
relation. This module collects the modeling-tier definitions per relation, validates them
against that relation's live schema, and hands the builder ready semantic fragments.

Nothing here is guessed. A definition that cannot be placed or typed is reported as
:class:`~canonic.ingestion.builder.SkippedEvidence`, and one marked for review lowers the
draft's confidence so it never auto-applies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import sqlglot
from pydantic import ValidationError
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify

from canonic.connectors.acquisition import normalized_type_of
from canonic.connectors.base import (
    AcquisitionTier,
    DefinitionEntityType,
    DefinitionEvidence,
    RelationSchema,
)
from canonic.ingestion.models import EvidenceItem, EvidenceKind

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["DefinitionIndex", "RelationDefinitions", "UnplacedDefinition"]

# Normalized column type → a SQL type sqlglot can annotate expressions with.
_SQL_TYPES: dict[str, str] = {
    "string": "TEXT",
    "int": "BIGINT",
    "decimal": "DECIMAL",
    "float": "DOUBLE",
    "bool": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "json": "JSON",
}


@dataclass(frozen=True)
class UnplacedDefinition:
    """A modeling definition the builder could not fold into any draft, and why."""

    source: str
    reason: str


@dataclass
class RelationDefinitions:
    """Ready semantic fragments for one relation, plus what they cover.

    ``dimension_names`` and ``dimension_columns`` tell the builder which inferred
    dimensions a modeling dimension replaces. ``join_columns`` are local columns a modeling
    join already covers, so no LLM join is drafted for them. ``review_notes`` lower the
    draft's confidence.
    """

    measures: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)
    grain: list[str] = field(default_factory=list)
    description: str | None = None
    review_notes: list[str] = field(default_factory=list)

    @property
    def dimension_names(self) -> set[str]:
        return {d["name"] for d in self.dimensions}

    @property
    def dimension_columns(self) -> set[str]:
        return {d["column"] for d in self.dimensions if "column" in d}

    def fingerprint(self) -> str | None:
        """Stable sha256 over everything these definitions contribute to a draft.

        ``None`` when no modeling definition reached the relation, so a run without the
        definition connector never looks like a change.
        """
        payload = {
            "measures": self.measures,
            "dimensions": self.dimensions,
            "joins": self.joins,
            "grain": self.grain,
            "description": self.description,
        }
        if not any(payload.values()):
            return None
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return f"sha256:{digest}"

    @property
    def join_columns(self) -> set[str]:
        return {
            column.name
            for join in self.joins
            for column in sqlglot.parse_one(join["on"]).find_all(exp.Column)
            if column.table != join["to"]
        }


def _relation_key(reference: str) -> str:
    """The short relation name the builder keys drafts by (``main.orders`` → ``orders``)."""
    return reference.split(".")[-1]


@dataclass(frozen=True)
class _Pending:
    item: EvidenceItem
    definition: DefinitionEvidence


class DefinitionIndex:
    """Collects modeling-tier definitions and resolves them against introspected relations.

    Usage: :meth:`add` every evidence item, :meth:`resolve` once with every relation
    introspected in the run, then :meth:`for_relation` per draft.
    """

    def __init__(self) -> None:
        self._pending: list[_Pending] = []
        self._invalid: list[UnplacedDefinition] = []
        self._resolved: dict[str, RelationDefinitions] = {}
        self._candidates: list[_Pending] = []
        self._measure_aliases: dict[str, list[str]] = {}

    def add(self, item: EvidenceItem) -> None:
        """Remember a modeling-tier ``definition`` item; ignore everything else."""
        if (
            item.kind != EvidenceKind.DEFINITION
            or item.acquisition_tier != AcquisitionTier.MODELING
        ):
            return
        try:
            definition = DefinitionEvidence.model_validate(item.payload)
        except ValidationError as exc:
            self._invalid.append(
                UnplacedDefinition(source=item.source, reason=f"invalid definition payload: {exc}")
            )
            return
        self._pending.append(_Pending(item=item, definition=definition))

    def resolve(self, relations: dict[str, RelationSchema]) -> list[UnplacedDefinition]:
        """Fold every remembered definition into its relation; return what could not be placed.

        ``relations`` maps short relation names to the schemas introspected in this run.
        """
        unplaced: list[UnplacedDefinition] = list(self._invalid)
        for pending in self._pending:
            if pending.definition.contract_candidate is not None:
                self._candidates.append(pending)
                continue
            reason = self._place(pending.definition, relations)
            if reason is not None:
                unplaced.append(UnplacedDefinition(source=pending.item.source, reason=reason))
        return unplaced

    def for_relation(self, name: str) -> RelationDefinitions:
        return self._resolved.get(name, RelationDefinitions())

    def candidates(self) -> list[tuple[str, DefinitionEvidence]]:
        """Metrics carrying a contract candidate, as ``(evidence source, definition)``."""
        return [(p.item.source, p.definition) for p in self._candidates]

    def placed_measure(self, name: str) -> tuple[str, dict[str, Any]] | None:
        """The relation a measure was placed on and its draft fragment, if it was placed."""
        for relation, bucket in self._resolved.items():
            for measure in bucket.measures:
                if measure["name"] == name:
                    return relation, measure
        return None

    def measure_aliases(self, name: str) -> list[str]:
        """Aliases the source stated for a measure (Ossie metric synonyms)."""
        return list(self._measure_aliases.get(name, []))

    def _bucket(self, name: str) -> RelationDefinitions:
        return self._resolved.setdefault(name, RelationDefinitions())

    def _place(
        self, definition: DefinitionEvidence, relations: dict[str, RelationSchema]
    ) -> str | None:
        """Place one definition. Returns a reason when it cannot be placed, else ``None``."""
        label = f"{definition.entity_type.value} {definition.entity!r} ({definition.native_ref})"
        match definition.entity_type:
            case DefinitionEntityType.MEASURE:
                return self._place_measure(definition, relations, label)
            case DefinitionEntityType.DIMENSION:
                return self._place_dimension(definition, relations, label)
            case DefinitionEntityType.JOIN:
                return self._place_join(definition, relations, label)
            case DefinitionEntityType.ENTITY:
                for ref in definition.references:
                    bucket = self._bucket(_relation_key(ref))
                    if definition.grain and not bucket.grain:
                        bucket.grain = list(definition.grain)
                return None
            case DefinitionEntityType.MODEL:
                if definition.description:
                    self._bucket(
                        _relation_key(definition.entity)
                    ).description = definition.description
                return None
            case _:
                return None

    def _single_relation(
        self, definition: DefinitionEvidence, relations: dict[str, RelationSchema], label: str
    ) -> tuple[str, RelationSchema] | str:
        """The one introspected relation a measure or dimension lives on, or a reason."""
        if len(definition.references) != 1:
            return f"{label} does not name exactly one relation"
        name = _relation_key(definition.references[0])
        schema = relations.get(name)
        if schema is None:
            return f"{label}: no introspected relation {name!r} in this run"
        return name, schema

    def _place_measure(
        self, definition: DefinitionEvidence, relations: dict[str, RelationSchema], label: str
    ) -> str | None:
        if definition.additivity is None:
            # A semantic measure has no "unknown" additivity, so drafting one would fail the
            # validation gate for the whole run.
            return f"{label} has unknown additivity; declare it by hand to use it"
        placed = self._single_relation(definition, relations, label)
        if isinstance(placed, str):
            return placed
        name, _ = placed
        bucket = self._bucket(name)
        entry = {
            "name": definition.entity,
            "expr": definition.expr or definition.entity,
            "additivity": definition.additivity.value,
        }
        existing = next((m for m in bucket.measures if m["name"] == definition.entity), None)
        if existing is not None:
            if existing == entry:
                return None
            bucket.review_notes.append(f"measure {definition.entity}: conflicting definitions")
            return f"{label} conflicts with an earlier definition of the same measure on {name!r}"
        bucket.measures.append(entry)
        if definition.aliases:
            self._measure_aliases.setdefault(definition.entity, list(definition.aliases))
        bucket.review_notes.extend(
            f"measure {definition.entity}: {f.value}" for f in definition.review_flags
        )
        return None

    def _place_dimension(
        self, definition: DefinitionEvidence, relations: dict[str, RelationSchema], label: str
    ) -> str | None:
        placed = self._single_relation(definition, relations, label)
        if isinstance(placed, str):
            return placed
        name, schema = placed
        columns = {c.name: c.type for c in schema.columns}
        fragment: dict[str, Any] = {"name": definition.entity}
        column = definition.column or _bare_column(definition.expr)
        if column is not None:
            if column not in columns:
                return f"{label}: column {column!r} is not on {name!r}"
            fragment["column"] = column
        elif definition.expr:
            referenced = _columns_of(definition.expr)
            if referenced is None:
                return f"{label}: expression does not parse"
            missing = sorted(referenced - columns.keys())
            if missing:
                return f"{label}: columns {missing} are not on {name!r}"
            value_type = _expression_type(definition.expr, columns)
            if value_type is None:
                return f"{label}: the expression's type cannot be inferred; declare it by hand"
            fragment["expr"] = definition.expr
            fragment["type"] = value_type
        else:
            return f"{label} has neither a column nor an expression"
        if definition.description:
            fragment["description"] = definition.description
        if definition.aliases:
            fragment["aliases"] = list(definition.aliases)
        bucket = self._bucket(name)
        existing = next((d for d in bucket.dimensions if d["name"] == definition.entity), None)
        if existing is not None:
            if existing == fragment:
                return None
            bucket.review_notes.append(f"dimension {definition.entity}: conflicting definitions")
            return f"{label} conflicts with an earlier definition of the same dimension on {name!r}"
        bucket.dimensions.append(fragment)
        return None

    def _place_join(
        self, definition: DefinitionEvidence, relations: dict[str, RelationSchema], label: str
    ) -> str | None:
        """Only joins that state their predicate are drafted, others stay ignored."""
        specs = [j for j in definition.joins if j.on]
        if not specs:
            return None
        for spec in specs:
            source, target = relations.get(spec.left), relations.get(spec.right)
            if source is None or target is None:
                missing = spec.left if source is None else spec.right
                return f"{label}: no introspected relation {missing!r} in this run"
            if source.connection != target.connection:
                return (
                    f"{label} crosses connections {source.connection!r} and {target.connection!r}"
                )
            assert spec.on is not None  # noqa: S101 — filtered above
            problem = _check_predicate(spec.on, {spec.left: source, spec.right: target})
            if problem is not None:
                return f"{label}: {problem}"
            bucket = self._bucket(spec.left)
            fragment = {"to": spec.right, "on": spec.on, "relationship": spec.relationship.value}
            if fragment not in bucket.joins:
                bucket.joins.append(fragment)
            bucket.review_notes.extend(
                f"join {definition.entity}: {f.value}" for f in definition.review_flags
            )
        return None


def _bare_column(expr: str | None) -> str | None:
    """The column name when ``expr`` is just a (possibly qualified) column, else ``None``."""
    if not expr:
        return None
    try:
        parsed = sqlglot.parse_one(expr)
    except SqlglotError:
        return None
    return parsed.name if isinstance(parsed, exp.Column) else None


def _columns_of(expr: str) -> set[str] | None:
    try:
        parsed = sqlglot.parse_one(expr)
    except SqlglotError:
        return None
    return {c.name for c in parsed.find_all(exp.Column)}


def _expression_type(expr: str, columns: dict[str, str]) -> str | None:
    """The normalized type of a derived value, inferred from its columns' types."""
    schema: dict[str, object] = {
        "t": {name: _SQL_TYPES[typ] for name, typ in columns.items() if typ in _SQL_TYPES}
    }
    try:
        query = qualify(sqlglot.parse_one(f"SELECT {expr} AS v FROM t"), schema=schema)
        annotated = annotate_types(query, schema=schema)
    except SqlglotError:
        return None
    selects = annotated.selects if isinstance(annotated, exp.Select) else []
    if not selects or selects[0].type is None:
        return None
    return normalized_type_of(selects[0].type.this)


def _check_predicate(on: str, sides: dict[str, RelationSchema]) -> str | None:
    """Every column in ``on`` must be qualified by one of the two relations and exist there."""
    try:
        columns: Iterable[exp.Column] = sqlglot.parse_one(on).find_all(exp.Column)
    except SqlglotError:
        return f"join predicate {on!r} does not parse"
    for column in columns:
        schema = sides.get(column.table)
        if schema is None:
            return f"join predicate column {column.sql()!r} is not qualified by either side"
        if column.name not in {c.name for c in schema.columns}:
            return f"join predicate column {column.sql()!r} does not exist"
    return None
