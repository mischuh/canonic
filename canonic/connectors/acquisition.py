"""Schema acquisition ladder and validation probe (SPEC-E2 §4, §5).

When live introspection (tier 1) is blocked or partial, descend the ladder to
declarative import (tier 4: DDL / schema YAML) or hand-authored semantics
(tier 6). Every supplemented relation is validated against the live source by a
zero-scan probe before its evidence is trusted; nothing is silently omitted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import sqlglot
from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML
from sqlglot import exp

from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    ConnectorBase,
    RelationSchema,
    SchemaIntrospectable,
    compute_fingerprint,
)
from canonic.exc import SchemaMismatch

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.semantic.models import SemanticSource

logger = logging.getLogger(__name__)

_T = exp.DataType.Type
# sqlglot canonical DDL type → normalized type set. sqlglot canonicalizes native
# spellings (``integer``→INT, ``numeric``→DECIMAL, ``real``→FLOAT), so we map its
# type enum rather than re-parsing rendered SQL.
_DDL_TYPE_MAP = {
    _T.TINYINT: "int",
    _T.SMALLINT: "int",
    _T.INT: "int",
    _T.BIGINT: "int",
    _T.DECIMAL: "decimal",
    _T.FLOAT: "float",
    _T.DOUBLE: "float",
    _T.BOOLEAN: "bool",
    _T.CHAR: "string",
    _T.VARCHAR: "string",
    _T.TEXT: "string",
    _T.UUID: "string",
    _T.DATE: "date",
    _T.TIMESTAMP: "timestamp",
    _T.TIMESTAMPTZ: "timestamp",
    _T.DATETIME: "timestamp",
    _T.JSON: "json",
    _T.JSONB: "json",
}


def _normalize_ddl_type(kind: exp.DataType | None, relation: str, column: str) -> str:
    """Map a parsed DDL column type to the normalized set; unmappable → json."""
    mapped = _DDL_TYPE_MAP.get(kind.this) if kind is not None else None
    if mapped is None:
        native = kind.sql(dialect="postgres") if kind is not None else "?"
        logger.warning("unmapped DDL type %r on %s.%s recorded as json", native, relation, column)
        return "json"
    return mapped


__all__ = [
    "AcquisitionLadder",
    "AcquisitionResult",
    "GapReport",
    "ProbeResult",
    "TypeConflict",
    "probe_schema",
    "relations_from_ddl",
    "relations_from_schema_yaml",
    "relations_from_semantic_sources",
]


class TypeConflict(BaseModel):
    """A column whose declared normalized type differs from the observed one."""

    model_config = ConfigDict(frozen=True)

    column: str
    declared: str
    observed: str


class ProbeResult(BaseModel):
    """Outcome of probe_schema(): the declared-vs-observed diff and, on success,
    the validated (stamped) RelationSchema.
    """

    model_config = ConfigDict(frozen=True)

    relation: str
    ok: bool
    missing_columns: list[str] = []  # declared but not observed
    extra_columns: list[str] = []  # observed but not declared (informational)
    type_conflicts: list[TypeConflict] = []
    validated: RelationSchema | None = None  # stamped copy when ok

    def raise_for_status(self) -> None:
        """Raise SchemaMismatch with a human-readable diff when not ok."""
        if self.ok:
            return
        parts: list[str] = []
        if self.missing_columns:
            parts.append(f"missing columns: {', '.join(self.missing_columns)}")
        if self.type_conflicts:
            conflicts = ", ".join(
                f"{c.column} (declared {c.declared}, observed {c.observed})"
                for c in self.type_conflicts
            )
            parts.append(f"type conflicts: {conflicts}")
        if self.extra_columns:
            parts.append(f"extra columns: {', '.join(self.extra_columns)}")
        raise SchemaMismatch(f"{self.relation}: {'; '.join(parts)}")


class GapReport(BaseModel):
    """Relations expected but not acquired by any tier; never silently omitted."""

    model_config = ConfigDict(frozen=True)

    missing_relations: list[str] = []

    @property
    def has_gap(self) -> bool:
        """True if any expected relation could not be acquired."""
        return bool(self.missing_relations)


class AcquisitionResult(BaseModel):
    """The acquired schema evidence plus a gap report for the caller to act on."""

    model_config = ConfigDict(frozen=True)

    schemas: list[RelationSchema]
    gap: GapReport


async def probe_schema(
    connector: SchemaIntrospectable, relation_schema: RelationSchema
) -> ProbeResult:
    """Validate a declared/hand-authored schema against the live source (§5).

    Observes the relation via a zero-scan probe and diffs declared columns/types
    against observed. A missing column or type conflict is a mismatch; extra live
    columns are reported but do not fail (a declared subset is legitimate). On a
    clean match, stamps ``last_validated_at`` and a fresh ``source_fingerprint``.
    """
    declared = {c.name: c.type for c in relation_schema.columns}
    try:
        observed_cols = await connector.describe_relation(relation_schema.relation)
    except Exception as exc:  # noqa: BLE001 — any failure to observe ⇒ relation unverified
        logger.warning("could not observe %s: %s", relation_schema.relation, exc)
        return ProbeResult(
            relation=relation_schema.relation, ok=False, missing_columns=sorted(declared)
        )

    observed = {c.name: c.type for c in observed_cols}
    missing = sorted(set(declared) - set(observed))
    extra = sorted(set(observed) - set(declared))
    conflicts = [
        TypeConflict(column=name, declared=declared[name], observed=observed[name])
        for name in sorted(set(declared) & set(observed))
        if declared[name] != observed[name]
    ]

    ok = not missing and not conflicts
    validated = (
        relation_schema.model_copy(
            update={
                "last_validated_at": datetime.now(UTC),
                "source_fingerprint": compute_fingerprint(
                    relation_schema.columns,
                    relation_schema.primary_key,
                    relation_schema.foreign_keys,
                ),
            }
        )
        if ok
        else None
    )
    return ProbeResult(
        relation=relation_schema.relation,
        ok=ok,
        missing_columns=missing,
        extra_columns=extra,
        type_conflicts=conflicts,
        validated=validated,
    )


def relations_from_ddl(ddl: str, connection: str) -> list[RelationSchema]:
    """Tier 4: parse CREATE TABLE DDL into declarative RelationSchema evidence."""
    relations: list[RelationSchema] = []
    for statement in sqlglot.parse(ddl, read="postgres"):
        if not isinstance(statement, exp.Create) or (statement.kind or "").upper() != "TABLE":
            continue
        schema_expr = statement.this
        if not isinstance(schema_expr, exp.Schema):  # CREATE TABLE without a column list
            continue
        table = schema_expr.this
        relation = f"{table.db}.{table.name}" if table.db else table.name
        coldefs = [e for e in schema_expr.expressions if isinstance(e, exp.ColumnDef)]

        primary_key: list[str] = []
        for e in schema_expr.expressions:
            if isinstance(e, exp.PrimaryKey):
                primary_key.extend(col.name for col in e.expressions)
        for coldef in coldefs:
            if any(isinstance(c.kind, exp.PrimaryKeyColumnConstraint) for c in coldef.constraints):
                primary_key.append(coldef.name)

        columns: list[ColumnInfo] = []
        for position, coldef in enumerate(coldefs, start=1):
            not_null = any(
                isinstance(c.kind, exp.NotNullColumnConstraint) for c in coldef.constraints
            )
            columns.append(
                ColumnInfo(
                    name=coldef.name,
                    type=_normalize_ddl_type(coldef.args.get("kind"), relation, coldef.name),
                    nullable=not (not_null or coldef.name in primary_key),
                    position=position,
                )
            )

        relations.append(
            RelationSchema(
                connection=connection,
                relation=relation,
                kind="table",
                columns=columns,
                primary_key=primary_key,
                acquisition_tier=AcquisitionTier.DECLARATIVE,
                source_fingerprint=compute_fingerprint(columns, primary_key, []),
            )
        )
    return relations


def relations_from_schema_yaml(path: Path) -> list[RelationSchema]:
    """Tier 4: load a RelationSchema-shaped YAML doc (single or list).

    The acquisition tier is forced to ``declarative`` regardless of what the file
    declares, and the fingerprint is recomputed over the normalized shape.
    """
    yaml = YAML()
    with open(path) as f:
        raw = yaml.load(f)
    docs = raw if isinstance(raw, list) else [raw]

    relations: list[RelationSchema] = []
    for doc in docs:
        schema = RelationSchema.model_validate(doc)
        relations.append(
            schema.model_copy(
                update={
                    "acquisition_tier": AcquisitionTier.DECLARATIVE,
                    "source_fingerprint": compute_fingerprint(
                        schema.columns, schema.primary_key, schema.foreign_keys
                    ),
                }
            )
        )
    return relations


def relations_from_semantic_sources(sources: list[SemanticSource]) -> list[RelationSchema]:
    """Tier 6: project hand-authored semantic sources to RelationSchema evidence."""
    relations: list[RelationSchema] = []
    for source in sources:
        columns = [
            ColumnInfo(name=c.name, type=c.type.value, nullable=c.nullable) for c in source.columns
        ]
        relations.append(
            RelationSchema(
                connection=source.connection,
                relation=source.table,
                kind="table",
                columns=columns,
                primary_key=source.grain,
                acquisition_tier=AcquisitionTier.HAND_AUTHORED,
                source_fingerprint=compute_fingerprint(columns, source.grain, []),
            )
        )
    return relations


class AcquisitionLadder:
    """Orchestrates the schema acquisition ladder for one connector (SPEC-E2 §4).

    Tries live introspection (tier 1), then supplements blocked/missing relations
    from declarative (tier 4) and hand-authored (tier 6) sources, probing each
    supplement against the live source. Live evidence always wins over a declared
    duplicate. Returns acquired schemas plus a gap report; it never prompts and
    never silently drops a relation.
    """

    def __init__(self, connector: ConnectorBase) -> None:
        self._connector = connector

    async def acquire(
        self,
        *,
        connection: str | None = None,
        expected_relations: list[str] | None = None,
        ddl: str | None = None,
        schema_yaml: Path | None = None,
        semantic_sources: list[SemanticSource] | None = None,
        probe: bool = True,
    ) -> AcquisitionResult:
        """Acquire schema evidence across the ladder, validating supplements.

        Raises SchemaMismatch if a probed supplement diverges from the live source.
        """
        acquired: dict[str, RelationSchema] = {}

        if Capability.INTROSPECT_SCHEMA in self._connector.capabilities():
            try:
                live = await cast("SchemaIntrospectable", self._connector).introspect_schema()
            except Exception as exc:  # noqa: BLE001 — blocked catalog ⇒ descend the ladder
                logger.warning("live introspection failed (%s); using declarative tiers", exc)
                live = []
            for schema in live:
                acquired[schema.relation] = schema

        supplements: list[RelationSchema] = []
        if ddl is not None:
            if connection is None:
                raise ValueError("connection is required to acquire schema from DDL")
            supplements.extend(relations_from_ddl(ddl, connection))
        if schema_yaml is not None:
            supplements.extend(relations_from_schema_yaml(schema_yaml))
        if semantic_sources:
            supplements.extend(relations_from_semantic_sources(semantic_sources))

        for schema in supplements:
            if schema.relation in acquired:
                continue  # live introspection wins
            if probe:
                result = await probe_schema(cast("SchemaIntrospectable", self._connector), schema)
                result.raise_for_status()
                assert result.validated is not None  # noqa: S101 — guaranteed by raise_for_status
                acquired[schema.relation] = result.validated
            else:
                acquired[schema.relation] = schema

        missing: list[str] = []
        if expected_relations is not None:
            missing = sorted(set(expected_relations) - set(acquired))

        schemas = [acquired[relation] for relation in sorted(acquired)]
        return AcquisitionResult(schemas=schemas, gap=GapReport(missing_relations=missing))
