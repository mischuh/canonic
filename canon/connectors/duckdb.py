"""DuckDB connector — file-based analytical database connector (SPEC-E2 §6, AMENDMENT-E2).

Implements the four P0 capabilities against DuckDB: ``capabilities``,
``test_connection``, ``introspect_schema`` (live, tier 1) and
``run_read_only_sql`` with parse-level and native read-only enforcement.

DuckDB's Python driver is synchronous; all blocking calls are wrapped with
``asyncio.to_thread`` to satisfy the async connector contract without blocking
the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb

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
    compute_fingerprint,
)
from canon.connectors.readonly import assert_read_only

if TYPE_CHECKING:
    from canon.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["DuckDBConnector"]

_DEFAULT_ROW_LIMIT = 10_000

_SAFE_RELATION = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_type(raw: str, relation: str, column: str) -> str:
    """Map a DuckDB declared type to the canonical normalized type set.

    Strips precision/scale before matching (e.g. ``DECIMAL(18,2)`` → ``DECIMAL``).
    Unmappable types fall back to ``json`` with a warning.
    """
    if not raw:
        return "json"
    t = re.sub(r"\(.*\)", "", raw.strip().upper()).strip()
    if t in {"BOOLEAN", "BOOL", "LOGICAL"}:
        return "bool"
    if t in {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "INT",
        "INT1",
        "INT2",
        "INT4",
        "BIGINT",
        "INT8",
        "HUGEINT",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
        "SIGNED",
    }:
        return "int"
    if t in {"FLOAT", "REAL", "FLOAT4", "DOUBLE", "FLOAT8", "DOUBLE PRECISION"}:
        return "float"
    if t in {"DECIMAL", "NUMERIC", "DEC"}:
        return "decimal"
    if t in {"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR", "CHARACTER VARYING", "CHARACTER"}:
        return "string"
    if t in {"UUID", "ENUM"}:
        return "string"
    if t in {"DATE"}:
        return "date"
    if t in {
        "TIMESTAMP",
        "TIMESTAMP WITH TIME ZONE",
        "TIMESTAMPTZ",
        "DATETIME",
        "TIMESTAMP_S",
        "TIMESTAMP_MS",
        "TIMESTAMP_NS",
    }:
        return "timestamp"
    if t in {"TIME", "TIME WITH TIME ZONE", "TIMETZ"}:
        return "string"
    if t in {"JSON", "BLOB", "BYTEA", "BINARY", "VARBINARY"}:
        return "json"
    if t in {"LIST", "MAP", "STRUCT", "UNION", "ARRAY"}:
        return "json"
    if t.startswith("INTERVAL"):
        return "string"
    logger.warning("unmapped DuckDB type %r on %s.%s recorded as json", raw, relation, column)
    return "json"


def _normalize_value_type(value: Any) -> str:
    """Best-effort normalized type name for a result value."""
    if isinstance(value, bool):
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


class DuckDBConnector(ConnectorBase):
    """Connector for DuckDB file databases (and ``:memory:`` for testing).

    Accepts a ``path`` param pointing to a ``.duckdb`` file.  The special
    value ``:memory:`` creates a transient in-memory database.

    All public methods run the synchronous DuckDB driver in a thread pool via
    ``asyncio.to_thread`` so the event loop is never blocked.
    """

    def __init__(self, connection: Connection) -> None:
        self._path: str = connection.params.get("path", ":memory:")
        self._row_limit: int = int(connection.params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._fetch_column_stats: bool = bool(connection.params.get("fetch_column_stats", False))
        self._connection_id: str = connection.id

    def capabilities(self) -> list[Capability]:
        return [
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        ]

    async def test_connection(self) -> Health:
        def _check() -> Health:
            try:
                con = duckdb.connect(self._path, read_only=self._path != ":memory:")
                con.execute("SELECT 1")
                con.close()
            except Exception as exc:  # by contract test_connection reports, never raises
                return Health(status="error", message=str(exc))
            return Health(status="ok")

        return await asyncio.to_thread(_check)

    async def introspect_schema(self) -> list[RelationSchema]:
        return await asyncio.to_thread(self._introspect_schema_sync)

    def _introspect_schema_sync(self) -> list[RelationSchema]:
        if self._fetch_column_stats:
            logger.warning(
                "fetch_column_stats=True requested on DuckDB connection %r, but DuckDB has no "
                "queryable planner statistics without a full table scan; ignoring (stats omitted)",
                self._connection_id,
            )
        read_only = self._path != ":memory:"
        con = duckdb.connect(self._path, read_only=read_only)
        try:
            tables = con.execute(
                """
                SELECT table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = 'main'
                ORDER BY table_name
                """
            ).fetchall()

            schemas: list[RelationSchema] = []
            for table_name, table_type in tables:
                cols = self._fetch_columns(con, table_name)
                if not cols:
                    continue
                pk = self._fetch_primary_key(con, table_name)
                fks = self._fetch_foreign_keys(con, table_name)
                relation = f"main.{table_name}"
                kind: str = "view" if table_type == "VIEW" else "table"
                schemas.append(
                    RelationSchema(
                        connection=self._connection_id,
                        relation=relation,
                        kind=kind,  # type: ignore[arg-type]
                        columns=cols,
                        primary_key=pk,
                        foreign_keys=fks,
                        row_count_estimate=None,
                        acquisition_tier=AcquisitionTier.LIVE,
                        source_fingerprint=compute_fingerprint(cols, pk, fks),
                    )
                )
        finally:
            con.close()
        return schemas

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        if not _SAFE_RELATION.match(relation):
            raise ValueError(f"unsafe relation identifier: {relation!r}")
        table_name = relation.split(".")[-1]

        def _run() -> list[ColumnInfo]:
            read_only = self._path != ":memory:"
            con = duckdb.connect(self._path, read_only=read_only)
            try:
                return self._fetch_columns(con, table_name)
            finally:
                con.close()

        return await asyncio.to_thread(_run)

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        assert_read_only(sql)
        return await asyncio.to_thread(self._run_sql_sync, sql)

    def _run_sql_sync(self, sql: str) -> ResultSet:
        read_only = self._path != ":memory:"
        con = duckdb.connect(self._path, read_only=read_only)
        try:
            result = con.execute(sql)
            column_desc = result.description
            fetched: list[Any] = result.fetchmany(self._row_limit + 1)
        finally:
            con.close()

        if not column_desc:
            return ResultSet(columns=[], rows=[], truncated=False)

        truncated = len(fetched) > self._row_limit
        rows = [list(row) for row in fetched[: self._row_limit]]
        keys = [desc[0] for desc in column_desc]
        columns = _result_columns(keys, rows)
        return ResultSet(columns=columns, rows=rows, truncated=truncated, bytes_scanned=None)

    def _fetch_columns(self, con: duckdb.DuckDBPyConnection, table_name: str) -> list[ColumnInfo]:
        rows = con.execute(
            """
            SELECT column_name, data_type, is_nullable, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = ?
            ORDER BY ordinal_position
            """,
            [table_name],
        ).fetchall()
        relation = f"main.{table_name}"
        return [
            ColumnInfo(
                name=row[0],
                type=_normalize_type(row[1], relation, row[0]),
                nullable=row[2].upper() == "YES",
                position=row[3],
            )
            for row in rows
        ]

    def _fetch_primary_key(self, con: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
        rows = con.execute(
            """
            SELECT constraint_column_names
            FROM duckdb_constraints()
            WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'
            """,
            [table_name],
        ).fetchall()
        if not rows:
            return []
        return list(rows[0][0])

    def _fetch_foreign_keys(
        self, con: duckdb.DuckDBPyConnection, table_name: str
    ) -> list[ForeignKey]:
        rows = con.execute(
            """
            SELECT constraint_column_names, referenced_table, referenced_column_names
            FROM duckdb_constraints()
            WHERE table_name = ? AND constraint_type = 'FOREIGN KEY'
              AND referenced_table IS NOT NULL
            """,
            [table_name],
        ).fetchall()

        fks: list[ForeignKey] = []
        for from_cols, ref_table, to_cols in rows:
            fks.append(
                ForeignKey(
                    columns=list(from_cols),
                    references=ForeignKeyRef(
                        relation=f"main.{ref_table}",
                        columns=list(to_cols),
                    ),
                )
            )
        return fks
