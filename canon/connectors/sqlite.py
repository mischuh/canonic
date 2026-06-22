"""SQLite connector — file-based SQL database connector (SPEC-E2 §6).

Implements the four P0 capabilities against SQLite: ``capabilities``,
``test_connection``, ``introspect_schema`` (live, tier 1) and
``run_read_only_sql`` with parse-level and URI-mode read-only enforcement.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import aiosqlite

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

__all__ = ["SQLiteConnector"]

_DEFAULT_ROW_LIMIT = 10_000

# Identifier safe to interpolate into PRAGMA calls (no schema prefix for table names).
_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# For describe_relation() which accepts "main.table" or bare "table".
_SAFE_RELATION = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*$")

_KIND_MAP: dict[str, str] = {"table": "table", "view": "view"}


def _normalize_type(raw: str, relation: str, column: str) -> str:
    """Map a SQLite declared type to the canonical normalized type set.

    Uses SQLite's type affinity rules (substring matching) per
    https://www.sqlite.org/datatype3.html §3.1.
    Unmappable types fall back to ``json`` with a warning.
    """
    if not raw:
        return "json"  # no declared type → BLOB affinity
    t = re.sub(r"\(.*\)", "", raw.strip().upper()).strip()
    if "BOOL" in t:
        return "bool"
    if "INT" in t:
        return "int"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return "string"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "float"
    if "BLOB" in t:
        return "json"
    if "JSON" in t:
        return "json"
    if "TIME" in t:  # DATETIME, TIMESTAMP, TIME all map to timestamp
        return "timestamp"
    if "DATE" in t:
        return "date"
    if "NUM" in t or "DEC" in t:
        return "decimal"
    if "UUID" in t or "NAME" in t:
        return "string"
    logger.warning("unmapped SQLite type %r on %s.%s recorded as json", raw, relation, column)
    return "json"


def _normalize_value_type(value: Any) -> str:
    """Best-effort normalized type name for a result value."""
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


class SQLiteConnector(ConnectorBase):
    """Connector for SQLite file databases.

    Accepts a ``path`` param pointing to a SQLite database file.  The special
    value ``:memory:`` creates a transient in-memory database (each connection
    call sees a fresh DB — useful only for testing and schema probing).
    """

    def __init__(self, connection: Connection) -> None:
        self._path: str = connection.params.get("path", ":memory:")
        self._row_limit: int = int(connection.params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._connection_id: str = connection.id

    def capabilities(self) -> list[Capability]:
        return [
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        ]

    async def test_connection(self) -> Health:
        try:
            async with aiosqlite.connect(self._path) as db:
                await db.execute("SELECT 1")
        except Exception as exc:  # by contract test_connection reports, never raises
            return Health(status="error", message=str(exc))
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
            relations = await cursor.fetchall()

            schemas: list[RelationSchema] = []
            for name, kind in relations:
                cols = await self._fetch_columns(db, name)
                if not cols:
                    continue
                pk = await self._fetch_primary_key(db, name)
                fks = await self._fetch_foreign_keys(db, name)
                relation = f"main.{name}"
                schemas.append(
                    RelationSchema(
                        connection=self._connection_id,
                        relation=relation,
                        kind=_KIND_MAP.get(kind, "table"),  # type: ignore[arg-type]
                        columns=cols,
                        primary_key=pk,
                        foreign_keys=fks,
                        row_count_estimate=None,
                        acquisition_tier=AcquisitionTier.LIVE,
                        source_fingerprint=compute_fingerprint(cols, pk, fks),
                    )
                )
        return schemas

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        """Column info via PRAGMA table_info — zero-scan, no rows read."""
        if not _SAFE_RELATION.match(relation):
            raise ValueError(f"unsafe relation identifier: {relation!r}")
        table_name = relation.split(".")[-1]
        async with aiosqlite.connect(self._path) as db:
            return await self._fetch_columns(db, table_name)

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        assert_read_only(sql)

        # Use URI read-only mode for real files; in-memory DBs don't support it.
        if self._path == ":memory:":
            path_arg, use_uri = self._path, False
        else:
            path_arg, use_uri = f"file:{self._path}?mode=ro", True

        async with aiosqlite.connect(path_arg, uri=use_uri) as db:
            cursor = await db.execute(sql)
            column_desc = cursor.description
            fetched: list[Any] = list(await cursor.fetchmany(self._row_limit + 1))

        truncated = len(fetched) > self._row_limit
        rows = [list(row) for row in fetched[: self._row_limit]]

        if not column_desc:
            return ResultSet(columns=[], rows=[], truncated=False)

        keys = [desc[0] for desc in column_desc]
        columns = _result_columns(keys, rows)
        return ResultSet(columns=columns, rows=rows, truncated=truncated, bytes_scanned=None)

    async def _fetch_columns(self, db: aiosqlite.Connection, name: str) -> list[ColumnInfo]:
        # PRAGMA doesn't support bound parameters; name is validated or from sqlite_master.
        cursor = await db.execute(f'PRAGMA table_info("{name}")')
        rows = await cursor.fetchall()
        # Row: (cid, name, type, notnull, dflt_value, pk)
        relation = f"main.{name}"
        return [
            ColumnInfo(
                name=row[1],
                type=_normalize_type(row[2], relation, row[1]),
                nullable=not bool(row[3]),
                position=row[0] + 1,
            )
            for row in rows
        ]

    async def _fetch_primary_key(self, db: aiosqlite.Connection, name: str) -> list[str]:
        cursor = await db.execute(f'PRAGMA table_info("{name}")')
        rows = await cursor.fetchall()
        # row[5] = pk column index (0 = not PK, ≥1 = PK position)
        pk_cols = [(row[5], row[1]) for row in rows if row[5] > 0]
        pk_cols.sort()
        return [col for _, col in pk_cols]

    async def _fetch_foreign_keys(self, db: aiosqlite.Connection, name: str) -> list[ForeignKey]:
        cursor = await db.execute(f'PRAGMA foreign_key_list("{name}")')
        rows = await cursor.fetchall()
        # Row: (id, seq, table, from, to, on_update, on_delete, match)
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            fk_id, seq, ref_table, from_col, to_col = (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
            )
            if fk_id not in grouped:
                grouped[fk_id] = {"ref_table": ref_table, "from_cols": [], "to_cols": []}
            grouped[fk_id]["from_cols"].append((seq, from_col))
            grouped[fk_id]["to_cols"].append((seq, to_col))

        fks: list[ForeignKey] = []
        for entry in grouped.values():
            from_cols = [c for _, c in sorted(entry["from_cols"])]
            to_cols = [c for _, c in sorted(entry["to_cols"])]
            fks.append(
                ForeignKey(
                    columns=from_cols,
                    references=ForeignKeyRef(
                        relation=f"main.{entry['ref_table']}",
                        columns=to_cols,
                    ),
                )
            )
        return fks


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
