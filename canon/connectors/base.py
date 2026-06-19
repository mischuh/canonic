"""Connector contract: capability enum, normalized evidence schema, and abstract base."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from canon.exc import CanonError, ConnectionError, ReadOnlyViolation, SchemaMismatch
from canon.semantic.models import (
    Additivity,  # noqa: TC001 — Pydantic resolves field annotations at runtime
    Relationship,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)

__all__ = [
    "AcquisitionTier",
    "CanonError",
    "Capability",
    "ColumnInfo",
    "ConnectorBase",
    "ConnectionError",
    "DefinitionEntityType",
    "DefinitionEvidence",
    "DefinitionExtract",
    "DocEvidence",
    "ForeignKey",
    "ForeignKeyRef",
    "Health",
    "JoinSpec",
    "ObservedQuery",
    "ReadOnlyViolation",
    "RelationSchema",
    "ResultColumn",
    "ResultSet",
    "SchemaMismatch",
    "UsageHint",
    "compute_fingerprint",
]


class Capability(StrEnum):
    """Capabilities a connector may advertise via capabilities()."""

    INTROSPECT_SCHEMA = "introspect_schema"
    READ_QUERY_HISTORY = "read_query_history"
    RUN_READ_ONLY_SQL = "run_read_only_sql"
    TEST_CONNECTION = "test_connection"
    CAPABILITIES = "capabilities"
    EXTRACT_DEFINITIONS = "extract_definitions"
    EXTRACT_EVIDENCE = "extract_evidence"


class AcquisitionTier(StrEnum):
    """Schema acquisition ladder tier that produced a RelationSchema."""

    LIVE = "live"
    MODELING = "modeling"
    QUERY_HISTORY = "query_history"
    DECLARATIVE = "declarative"
    SAMPLE = "sample"
    HAND_AUTHORED = "hand_authored"


class Health(BaseModel):
    """Result of test_connection()."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "error"]
    message: str | None = None


class ColumnInfo(BaseModel):
    """A single column in a RelationSchema."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str  # normalized type set: string, int, decimal, float, bool, date, timestamp, json
    nullable: bool = True
    position: int | None = None


class ForeignKeyRef(BaseModel):
    """The target side of a foreign-key relationship."""

    model_config = ConfigDict(frozen=True)

    relation: str
    columns: list[str]


class ForeignKey(BaseModel):
    """A single foreign-key constraint discovered on a relation."""

    model_config = ConfigDict(frozen=True)

    columns: list[str]
    references: ForeignKeyRef


class RelationSchema(BaseModel):
    """Normalized schema evidence emitted by introspect_schema()."""

    model_config = ConfigDict(frozen=True)

    connection: str
    relation: str  # fully-qualified, e.g. analytics.fct_orders
    kind: Literal["table", "view", "materialized_view"]
    columns: list[ColumnInfo]
    primary_key: list[str] = []
    foreign_keys: list[ForeignKey] = []
    row_count_estimate: int | None = None
    acquisition_tier: AcquisitionTier
    source_fingerprint: str | None = None  # sha256 over the normalized schema for drift detection
    last_validated_at: datetime | None = None  # stamped by the schema validation probe (§5)


class ObservedQuery(BaseModel):
    """[P1] Normalized query-history evidence emitted by read_query_history()."""

    model_config = ConfigDict(frozen=True)

    sql_normalized: str
    relations: list[str] = []
    joins_observed: list[dict[str, str]] = []
    frequency: int = 0
    last_seen: datetime | None = None


class ResultColumn(BaseModel):
    """A single column descriptor in a ResultSet."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str


class ResultSet(BaseModel):
    """Normalized query result emitted by run_read_only_sql()."""

    model_config = ConfigDict(frozen=True)

    columns: list[ResultColumn]
    rows: list[list[Any]]
    truncated: bool = False
    bytes_scanned: int | None = None  # nullable; feeds cost control (E13)


def compute_fingerprint(
    columns: list[ColumnInfo], primary_key: list[str], foreign_keys: list[ForeignKey]
) -> str:
    """Stable sha256 over the normalized schema, for drift detection (§2.1).

    Shared by every acquisition tier so a relation acquired live, declaratively,
    or by hand fingerprints identically when its normalized shape matches.
    """
    payload = {
        "columns": [c.model_dump() for c in columns],
        "primary_key": primary_key,
        "foreign_keys": [fk.model_dump() for fk in foreign_keys],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


class DefinitionEntityType(StrEnum):
    """The kind of semantic entity a DefinitionEvidence record describes (SPEC-E3 §3.1)."""

    MEASURE = "measure"
    DIMENSION = "dimension"
    MODEL = "model"
    JOIN = "join"
    ENTITY = "entity"


class JoinSpec(BaseModel):
    """One side of a join relationship within a DefinitionEvidence record."""

    model_config = ConfigDict(frozen=True)

    left: str
    right: str
    relationship: Relationship


class DefinitionEvidence(BaseModel):
    """Normalized definition evidence from a definition connector (SPEC-E3 §3.1).

    Carries the semantic intent of a modeling artifact (measure, join, model, etc.)
    in Canon's normalized shape so no vendor-specific structure reaches E4.
    ``native_ref`` is the vendor back-pointer (e.g. dbt ``unique_id``) for provenance.
    ``additivity=None`` encodes the spec's ``unknown`` value for unrecognized aggregations.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    entity: str
    entity_type: DefinitionEntityType
    expr: str | None = None
    additivity: Additivity | None = None
    references: list[str] = []
    grain: list[str] = []
    joins: list[JoinSpec] = []
    description: str | None = None
    native_ref: str
    acquisition_tier: AcquisitionTier
    source_fingerprint: str | None = None


class DefinitionExtract(BaseModel):
    """The combined output of extract_definitions() — schemas and definitions (SPEC-E3 §2)."""

    model_config = ConfigDict(frozen=True)

    relations: list[RelationSchema] = []
    definitions: list[DefinitionEvidence] = []


class UsageHint(StrEnum):
    """How a doc-evidence page should be used by E6 (SPEC-E3 §3.2, §5).

    Values intentionally mirror ``canon.knowledge.models.UsageMode`` so E6 maps
    ``usage_hint`` → ``usage_mode`` 1:1 without a round-trip through connectors.
    The two enums are kept parallel (not imported from each other) to preserve the
    correct dependency direction: knowledge → connectors is not allowed.
    """

    REFERENCE = "reference"
    CAVEAT = "caveat"
    POLICY = "policy"
    DEFINITION = "definition"


class DocEvidence(BaseModel):
    """Normalized prose evidence from an evidence connector (SPEC-E3 §3.2, §5).

    ``usage_hint`` maps to E6 ``usage_mode`` so a caveat in Notion becomes a
    ``caveat`` knowledge page.  ``topic_refs`` are *candidates* — unresolved ones
    are surfaced for review on write (E6 §3.1), never written as broken refs.
    ``native_ref`` carries the vendor back-pointer for provenance (e.g. ``notion:page:<id>``).
    """

    model_config = ConfigDict(frozen=True)

    source: str
    kind: Literal["doc_evidence"] = "doc_evidence"
    title: str
    body: str
    topic_refs: list[str] = []
    usage_hint: UsageHint
    native_ref: str
    source_fingerprint: str | None = None
    observed_at: datetime


class ConnectorBase(ABC):
    """Abstract base class for all Canon connectors.

    Connectors declare their capabilities via capabilities() and implement only
    the methods they support. Core dispatches on Capability values, never on
    vendor identity.
    """

    @abstractmethod
    def capabilities(self) -> list[Capability]:
        """Return the capabilities this connector implements."""

    @abstractmethod
    async def test_connection(self) -> Health:
        """Test reachability and credentials; return Health."""

    async def introspect_schema(self) -> list[RelationSchema]:
        """Return normalized schema evidence for all discoverable relations."""
        raise NotImplementedError(f"{type(self).__name__} does not support introspect_schema")

    async def read_query_history(self, since: datetime) -> list[ObservedQuery]:
        """[P1] Return observed queries executed since the given datetime."""
        raise NotImplementedError(f"{type(self).__name__} does not support read_query_history")

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        """Execute a read-only SELECT and return a normalized ResultSet."""
        raise NotImplementedError(f"{type(self).__name__} does not support run_read_only_sql")

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        """Observe a relation's columns with zero data scanned (SPEC-E2 §5).

        Backs the schema validation probe: a ``SELECT … WHERE false`` against the
        live source whose result description yields observed column names and
        normalized types, used to diff declared-vs-observed schema even when
        catalog introspection is blocked.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support describe_relation")

    async def extract_definitions(self) -> DefinitionExtract:
        """Extract normalized definition evidence from this source (SPEC-E3 §2, §4).

        Returns a :class:`DefinitionExtract` with both relation schemas (tier 2) and
        definition evidence records. Definition connectors implement this; primary
        connectors (E2) do not.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support extract_definitions")

    async def extract_evidence(self) -> list[DocEvidence]:
        """Extract normalized prose evidence from this source (SPEC-E3 §3.2, §5).

        Evidence connectors (e.g. Notion, arbitrary text) implement this; definition
        and primary connectors do not.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support extract_evidence")

    async def aclose(self) -> None:  # noqa: B027 — intentional no-op default; stateful subclasses override
        """Release any held resources (connection pools, sockets).

        The default implementation is a no-op; stateful connectors override it.
        Called by the core after each use so callers never manage engine lifecycles.
        """
