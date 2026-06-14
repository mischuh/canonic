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

__all__ = [
    "AcquisitionTier",
    "CanonError",
    "Capability",
    "ColumnInfo",
    "ConnectorBase",
    "ConnectionError",
    "ForeignKey",
    "ForeignKeyRef",
    "Health",
    "ObservedQuery",
    "ReadOnlyViolation",
    "RelationSchema",
    "ResultColumn",
    "ResultSet",
    "SchemaMismatch",
    "compute_fingerprint",
]


class Capability(StrEnum):
    """Capabilities a connector may advertise via capabilities()."""

    INTROSPECT_SCHEMA = "introspect_schema"
    READ_QUERY_HISTORY = "read_query_history"
    RUN_READ_ONLY_SQL = "run_read_only_sql"
    TEST_CONNECTION = "test_connection"
    CAPABILITIES = "capabilities"


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
