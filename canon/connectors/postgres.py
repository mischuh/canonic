"""PostgreSQL connector — the single Phase-0 concrete connector (SPEC-E2 §6).

Implements the four P0 capabilities against PostgreSQL: ``capabilities``,
``test_connection``, ``introspect_schema`` (live, tier 1) and
``run_read_only_sql`` with defense-in-depth read-only enforcement (SPEC-E2 §3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from canon.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    ConnectorBase,
    ForeignKey,
    ForeignKeyRef,
    Health,
    RelationSchema,
    ResultColumn,
    ResultSet,
)
from canon.credentials import resolve_credential
from canon.exc import ReadOnlyViolation

if TYPE_CHECKING:
    from canon.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["PostgresConnector"]

_DEFAULT_ROW_LIMIT = 10_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000

# Native Postgres type (parameter- and array-stripped, lower-cased) → normalized
# type set: string, int, decimal, float, bool, date, timestamp, json.
_PG_TYPE_MAP: dict[str, str] = {
    "smallint": "int",
    "integer": "int",
    "bigint": "int",
    "int2": "int",
    "int4": "int",
    "int8": "int",
    "numeric": "decimal",
    "decimal": "decimal",
    "real": "float",
    "double precision": "float",
    "float4": "float",
    "float8": "float",
    "boolean": "bool",
    "bool": "bool",
    "character varying": "string",
    "varchar": "string",
    "character": "string",
    "char": "string",
    "bpchar": "string",
    "text": "string",
    "name": "string",
    "uuid": "string",
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "json": "json",
    "jsonb": "json",
}

_KIND_BY_TABLE_TYPE = {"BASE TABLE": "table", "VIEW": "view"}


def _normalize_type(raw: str, relation: str, column: str) -> str:
    """Map a native Postgres type name to the normalized type set.

    Unmappable types (arrays, enums, vendor-specific) are recorded as ``json``
    with a warning, never dropped silently (SPEC-E2 §2.1, S2 AC2).
    """
    t = raw.strip().lower()
    if t.endswith("[]") or t == "array":
        logger.warning("array type %r on %s.%s recorded as json", raw, relation, column)
        return "json"
    if t in ("user-defined", "anyarray"):
        logger.warning("unmapped type %r on %s.%s recorded as json", raw, relation, column)
        return "json"
    t = re.sub(r"\(.*\)", "", t).strip()  # drop size/precision parameters
    mapped = _PG_TYPE_MAP.get(t)
    if mapped is None:
        logger.warning("unmapped Postgres type %r on %s.%s recorded as json", raw, relation, column)
        return "json"
    return mapped


def _fingerprint(
    columns: list[ColumnInfo], primary_key: list[str], foreign_keys: list[ForeignKey]
) -> str:
    """Stable sha256 over the normalized schema, for drift detection."""
    payload = {
        "columns": [c.model_dump() for c in columns],
        "primary_key": primary_key,
        "foreign_keys": [fk.model_dump() for fk in foreign_keys],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


def _normalize_value_type(value: Any) -> str:
    """Best-effort normalized type name for a result value (P0)."""
    if isinstance(value, bool):  # bool is a subclass of int — check first
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, Decimal):
        return "decimal"
    if isinstance(value, datetime):
        return "timestamp"
    if isinstance(value, date):
        return "date"
    if isinstance(value, (dict, list)):
        return "json"
    return "string"


class PostgresConnector(ConnectorBase):
    """Primary (queryable) connector for PostgreSQL and compatible engines."""

    def __init__(self, connection: Connection) -> None:
        params = connection.params
        password = resolve_credential(connection.credentials_ref)
        self._url = URL.create(
            "postgresql+asyncpg",
            username=params.get("user"),
            password=password,
            host=params.get("host"),
            port=params.get("port"),
            database=params.get("dbname") or params.get("database"),
        )
        self._row_limit = int(params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._statement_timeout_ms = int(
            params.get("statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS)
        )
        self._connection_id = connection.id
        self._engine: AsyncEngine | None = None

    @property
    def dsn(self) -> str:
        """The async SQLAlchemy DSN (password rendered, for diagnostics/tests)."""
        return self._url.render_as_string(hide_password=False)

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(self._url)
        return self._engine

    async def aclose(self) -> None:
        """Dispose the underlying engine and its connection pool."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def capabilities(self) -> list[Capability]:
        return [
            Capability.introspect_schema,
            Capability.run_read_only_sql,
            Capability.test_connection,
            Capability.capabilities,
        ]

    async def test_connection(self) -> Health:
        try:
            engine = self._get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # by contract test_connection reports, never raises
            return Health(status="error", message=str(exc))
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        engine = self._get_engine()
        async with engine.connect() as conn:
            relations = await self._fetch_relations(conn)
            columns = await self._fetch_columns(conn)
            primary_keys = await self._fetch_primary_keys(conn)
            foreign_keys = await self._fetch_foreign_keys(conn)
            row_estimates = await self._fetch_row_estimates(conn)

        schemas: list[RelationSchema] = []
        for (schema, name), kind in sorted(relations.items()):
            relation = f"{schema}.{name}"
            cols = columns.get((schema, name), [])
            if not cols:
                continue
            pk = primary_keys.get((schema, name), [])
            fks = foreign_keys.get((schema, name), [])
            estimate = row_estimates.get((schema, name))
            schemas.append(
                RelationSchema(
                    connection=self._connection_id,
                    relation=relation,
                    kind=kind,  # type: ignore[arg-type]
                    columns=cols,
                    primary_key=pk,
                    foreign_keys=fks,
                    row_count_estimate=estimate,
                    acquisition_tier=AcquisitionTier.live,
                    source_fingerprint=_fingerprint(cols, pk, fks),
                )
            )
        return schemas

    async def _fetch_relations(self, conn: Any) -> dict[tuple[str, str], str]:
        result = await conn.execute(
            text(
                "SELECT table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
            )
        )
        relations: dict[tuple[str, str], str] = {}
        for schema, name, table_type in result:
            kind = _KIND_BY_TABLE_TYPE.get(table_type)
            if kind is not None:
                relations[(schema, name)] = kind
        matviews = await conn.execute(
            text(
                "SELECT schemaname, matviewname FROM pg_matviews "
                "WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
            )
        )
        for schema, name in matviews:
            relations[(schema, name)] = "materialized_view"
        return relations

    async def _fetch_columns(self, conn: Any) -> dict[tuple[str, str], list[ColumnInfo]]:
        # information_schema covers tables and views; materialized-view columns
        # are not exposed there, so supplement via pg_catalog.
        out: dict[tuple[str, str], list[ColumnInfo]] = {}
        result = await conn.execute(
            text(
                "SELECT table_schema, table_name, column_name, data_type, "
                "is_nullable, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY table_schema, table_name, ordinal_position"
            )
        )
        for schema, name, column, data_type, is_nullable, position in result:
            relation = f"{schema}.{name}"
            out.setdefault((schema, name), []).append(
                ColumnInfo(
                    name=column,
                    type=_normalize_type(data_type, relation, column),
                    nullable=(is_nullable == "YES"),
                    position=position,
                )
            )
        matview_cols = await conn.execute(
            text(
                "SELECT n.nspname, c.relname, a.attname, "
                "format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attnum "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'm' AND a.attnum > 0 AND NOT a.attisdropped "
                "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY n.nspname, c.relname, a.attnum"
            )
        )
        for schema, name, column, data_type, attnotnull, position in matview_cols:
            relation = f"{schema}.{name}"
            out.setdefault((schema, name), []).append(
                ColumnInfo(
                    name=column,
                    type=_normalize_type(data_type, relation, column),
                    nullable=not attnotnull,
                    position=position,
                )
            )
        return out

    async def _fetch_primary_keys(self, conn: Any) -> dict[tuple[str, str], list[str]]:
        result = await conn.execute(
            text(
                "SELECT tc.table_schema, tc.table_name, kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                "  AND tc.table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position"
            )
        )
        out: dict[tuple[str, str], list[str]] = {}
        for schema, name, column in result:
            out.setdefault((schema, name), []).append(column)
        return out

    async def _fetch_foreign_keys(self, conn: Any) -> dict[tuple[str, str], list[ForeignKey]]:
        result = await conn.execute(
            text(
                "SELECT tc.table_schema, tc.table_name, tc.constraint_name, "
                "  kcu.column_name, ccu.table_schema, ccu.table_name, ccu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "  AND tc.table_schema = ccu.table_schema "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "  AND tc.table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position"
            )
        )
        # Group rows per (relation, constraint), preserving column order.
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for schema, name, constraint, column, ref_schema, ref_table, ref_column in result:
            entry = grouped.setdefault(
                (schema, name, constraint),
                {"columns": [], "ref_relation": f"{ref_schema}.{ref_table}", "ref_columns": []},
            )
            entry["columns"].append(column)
            entry["ref_columns"].append(ref_column)

        out: dict[tuple[str, str], list[ForeignKey]] = {}
        for (schema, name, _constraint), entry in grouped.items():
            out.setdefault((schema, name), []).append(
                ForeignKey(
                    columns=entry["columns"],
                    references=ForeignKeyRef(
                        relation=entry["ref_relation"], columns=entry["ref_columns"]
                    ),
                )
            )
        return out

    async def _fetch_row_estimates(self, conn: Any) -> dict[tuple[str, str], int | None]:
        result = await conn.execute(
            text(
                "SELECT n.nspname, c.relname, c.reltuples::bigint "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'v', 'm', 'p') "
                "  AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
            )
        )
        out: dict[tuple[str, str], int | None] = {}
        for schema, name, reltuples in result:
            out[(schema, name)] = (
                int(reltuples) if reltuples is not None and reltuples >= 0 else None
            )
        return out

    def _assert_read_only(self, sql: str) -> None:
        """Parse-level read-only check (SPEC-E2 §3.2); raises before execution."""
        try:
            statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
        except ParseError as exc:
            raise ReadOnlyViolation(f"could not parse SQL as read-only: {exc}") from exc
        if len(statements) != 1:
            raise ReadOnlyViolation(f"exactly one statement is allowed, got {len(statements)}")
        stmt = statements[0]
        if not isinstance(stmt, (exp.Select, exp.Union)):
            raise ReadOnlyViolation(
                f"only SELECT statements are permitted, got {type(stmt).__name__}"
            )

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        self._assert_read_only(sql)
        engine = self._get_engine()
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SET LOCAL default_transaction_read_only = on"))
            await conn.execute(text(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}"))
            result = await conn.stream(text(sql))
            keys = list(result.keys())
            fetched = await result.fetchmany(self._row_limit + 1)

        truncated = len(fetched) > self._row_limit
        rows = [list(row) for row in fetched[: self._row_limit]]
        columns = self._result_columns(keys, rows)
        return ResultSet(columns=columns, rows=rows, truncated=truncated, bytes_scanned=None)

    @staticmethod
    def _result_columns(keys: list[str], rows: list[list[Any]]) -> list[ResultColumn]:
        columns: list[ResultColumn] = []
        for idx, key in enumerate(keys):
            col_type = "string"
            for row in rows:
                if row[idx] is not None:
                    col_type = _normalize_value_type(row[idx])
                    break
            columns.append(ResultColumn(name=key, type=col_type))
        return columns
