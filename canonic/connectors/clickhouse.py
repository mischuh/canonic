"""ClickHouse connector (queryable).

Implements ``capabilities``, ``test_connection``, ``introspect_schema`` (live, tier 1) and
``run_read_only_sql`` with native read-only enforcement. In ClickHouse a "schema" is a database,
so relations are named ``database.table``. ``params["schemas"]``/``params["tables"]`` narrow the
relations that introspection returns (see ``canonic.connectors.relation_filter``).

The driver is clickhouse-connect (HTTP), which is synchronous, so every call runs on a worker
thread through ``asyncio.to_thread``, the same way the MySQL and Snowflake connectors do. It is an
optional dependency, imported when a connection is opened, with an install hint if it is missing.

ClickHouse has no foreign keys, and its primary key is a sorting key that does not make rows
unique. Introspection therefore reports no primary key, so the grain of a source is drafted and
marked as a draft instead of being asserted from a key that may repeat.

Queries run with ``readonly = 2``: writes and DDL are refused by the server, while settings the
compiled SQL carries (``SETTINGS join_use_nulls = 1``, see ``ClickHouseDialectAdapter``) stay
allowed. ``readonly = 1`` would reject that clause.

Params: ``host``, ``port`` (default 8123, or 8443 with ``secure``), ``user`` (default
``default``), ``database`` (optional, ``dbname`` is an alias), ``secure`` (bool, HTTPS with
certificate verification), ``schemas``, ``tables``, ``row_limit`` and ``statement_timeout_ms``.
The password comes from ``credentials_ref``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import re
from typing import TYPE_CHECKING, Any

from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    ConnectorBase,
    Health,
    ReadOnlyEnforcement,
    RelationSchema,
    ResultColumn,
    ResultSet,
    compute_fingerprint,
)
from canonic.connectors.readonly import assert_read_only
from canonic.connectors.relation_filter import filter_relations
from canonic.credentials import resolve_credential
from canonic.exc import ConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["ClickHouseConnector"]

_DEFAULT_PORT = 8123
_DEFAULT_SECURE_PORT = 8443
_DEFAULT_USER = "default"
_DEFAULT_ROW_LIMIT = 10_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
_CONNECT_TIMEOUT_S = 10
# Socket read timeout beyond the statement timeout, so a server that never answers raises
# instead of blocking a worker thread for good. The server enforces the statement timeout itself.
_READ_TIMEOUT_GRACE_S = 10
# ``readonly = 2`` refuses writes and DDL but still lets a query change settings, which the
# compiled SQL needs for ``join_use_nulls``.
_READONLY_LEVEL = 2

_SYSTEM_DATABASES = ("system", "information_schema", "INFORMATION_SCHEMA")

_VIEW_ENGINES = frozenset({"View", "MaterializedView", "LiveView", "WindowView"})

# Type wrappers that do not change what a column holds. ``Nullable`` is read separately.
_WRAPPERS = ("Nullable(", "LowCardinality(")

_INTEGER_TYPE = re.compile(r"^u?int(8|16|32|64|128|256)$")

# Bare type name (lower-cased, parameters dropped) -> normalized type set.
_CLICKHOUSE_TYPE_MAP: dict[str, str] = {
    "decimal": "decimal",
    "decimal32": "decimal",
    "decimal64": "decimal",
    "decimal128": "decimal",
    "decimal256": "decimal",
    "float32": "float",
    "float64": "float",
    "bfloat16": "float",
    "bool": "bool",
    "string": "string",
    "fixedstring": "string",
    "uuid": "string",
    "ipv4": "string",
    "ipv6": "string",
    "enum8": "string",
    "enum16": "string",
    "time": "string",
    "time64": "string",
    "date": "date",
    "date32": "date",
    "datetime": "timestamp",
    "datetime64": "timestamp",
    "json": "json",
}


def _require_driver() -> Any:
    """Import the ClickHouse driver, translating its absence into an actionable error."""
    try:
        return importlib.import_module("clickhouse_connect")
    except ImportError as exc:
        raise ConnectionError(
            "connection type 'clickhouse' requires clickhouse-connect, which is not installed. "
            "Install it with: pip install 'canonic[clickhouse]' (or pip install clickhouse-connect)"
        ) from exc


def _unwrap_type(type_name: str) -> tuple[str, bool]:
    """Strip ``Nullable(...)`` and ``LowCardinality(...)`` from ``type_name``.

    Returns the inner type and whether the column may hold NULL.
    """
    nullable = False
    text = type_name.strip()
    unwrapped = True
    while unwrapped:
        unwrapped = False
        for wrapper in _WRAPPERS:
            if text.startswith(wrapper) and text.endswith(")"):
                nullable = nullable or wrapper == "Nullable("
                text = text[len(wrapper) : -1].strip()
                unwrapped = True
    return text, nullable


def _normalize_type(type_name: str) -> str | None:
    """Map a ClickHouse column type to the normalized type set, or ``None`` if it has no mapping."""
    inner, _ = _unwrap_type(type_name)
    base = inner.split("(", 1)[0].strip().lower()
    if _INTEGER_TYPE.match(base):
        return "int"
    return _CLICKHOUSE_TYPE_MAP.get(base)


def _column_type(type_name: str, relation: str, column: str) -> str:
    """Normalized type of an introspected column. Unmappable types (arrays, maps, tuples) are
    recorded as ``json`` with a warning, never dropped silently."""
    mapped = _normalize_type(type_name)
    if mapped is None:
        logger.warning(
            "unmapped ClickHouse type %r on %s.%s recorded as json", type_name, relation, column
        )
        return "json"
    return mapped


def _as_bool(value: Any) -> bool:
    """Read a boolean param, which arrives as the string ``"false"`` from ``--param secure=false``."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _split_relation(relation: str) -> tuple[str, str]:
    """Split ``database.table`` (or a bare ``table``) into its parts, rejecting odd shapes."""
    database, _, name = relation.rpartition(".")
    if not name or "." in database:
        raise ValueError(f"unsafe relation identifier: {relation!r}")
    return database, name


class ClickHouseConnector(ConnectorBase):
    """Primary (queryable) connector for ClickHouse."""

    def __init__(self, connection: Connection) -> None:
        params = connection.params
        self._host = params.get("host")
        self._secure = _as_bool(params.get("secure", False))
        default_port = _DEFAULT_SECURE_PORT if self._secure else _DEFAULT_PORT
        self._port = int(params.get("port", default_port))
        self._user = params.get("user", _DEFAULT_USER)
        self._database = params.get("database") or params.get("dbname")
        self._password = resolve_credential(connection.credentials_ref)
        self._schemas_filter: list[str] | None = params.get("schemas")
        self._tables_filter: list[str] | None = params.get("tables")
        self._row_limit = int(params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._statement_timeout_ms = int(
            params.get("statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS)
        )
        if params.get("fetch_column_stats"):
            logger.warning("fetch_column_stats is not supported for ClickHouse and is ignored")
        self._connection_id = connection.id

    def _connect(self) -> Any:
        """Open a session. Blocking, call from a worker thread."""
        clickhouse_connect = _require_driver()
        return clickhouse_connect.get_client(
            host=self._host,
            port=self._port,
            username=self._user,
            password=self._password,
            database=self._database or "default",
            secure=self._secure,
            connect_timeout=_CONNECT_TIMEOUT_S,
            send_receive_timeout=self._statement_timeout_ms / 1000 + _READ_TIMEOUT_GRACE_S,
            autogenerate_session_id=False,
        )

    async def _in_session[T](self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(client)`` on a worker thread with a freshly opened client."""

        def _run() -> T:
            client = self._connect()
            try:
                return fn(client)
            finally:
                try:
                    client.close()
                except Exception:  # the connection may already be gone after a failed query
                    logger.debug("closing the ClickHouse client failed", exc_info=True)

        return await asyncio.to_thread(_run)

    def capabilities(self) -> list[Capability]:
        return [
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        ]

    async def test_connection(self) -> Health:
        try:
            await self._in_session(lambda client: client.query("SELECT version()").result_rows)
        except Exception as exc:  # by contract test_connection reports, never raises
            return Health(status="error", message=str(exc))
        return Health(status="ok")

    def read_only_enforcement(self) -> ReadOnlyEnforcement:
        return ReadOnlyEnforcement.NATIVE

    async def introspect_schema(self) -> list[RelationSchema]:
        return await self._in_session(self._introspect_sync)

    def _introspect_sync(self, client: Any) -> list[RelationSchema]:
        relations = _fetch_relations(client)
        row_estimates = {key: estimate for key, (_, estimate) in relations.items()}
        kinds = filter_relations(
            {key: kind for key, (kind, _) in relations.items()},
            self._schemas_filter,
            self._tables_filter,
        )
        columns = _fetch_columns(client)

        schemas: list[RelationSchema] = []
        for (database, name), kind in sorted(kinds.items()):
            cols = columns.get((database, name), [])
            if not cols:
                continue
            schemas.append(
                RelationSchema(
                    connection=self._connection_id,
                    relation=f"{database}.{name}",
                    kind=kind,  # type: ignore[arg-type]
                    columns=cols,
                    primary_key=[],
                    foreign_keys=[],
                    row_count_estimate=row_estimates.get((database, name)),
                    acquisition_tier=AcquisitionTier.LIVE,
                    source_fingerprint=compute_fingerprint(cols, [], []),
                )
            )
        return schemas

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        assert_read_only(sql, dialect="clickhouse")
        return await self._in_session(lambda client: self._run_sql_sync(client, sql))

    def _run_sql_sync(self, client: Any, sql: str) -> ResultSet:
        settings = {
            "readonly": _READONLY_LEVEL,
            "max_execution_time": max(1, math.ceil(self._statement_timeout_ms / 1000)),
        }
        # Streamed in blocks, so a huge result is never held in memory beyond ``row_limit`` rows.
        # Leaving the stream early closes the HTTP response and the server aborts the statement.
        rows: list[list[Any]] = []
        truncated = False
        with client.query_row_block_stream(sql, settings=settings) as stream:
            names = list(stream.source.column_names)
            types = [column_type.name for column_type in stream.source.column_types]
            for block in stream:
                for row in block:
                    if len(rows) == self._row_limit:
                        truncated = True
                        break
                    rows.append(list(row))
                if truncated:
                    break
        columns = [
            ResultColumn(name=name, type=_normalize_type(type_name) or "json")
            for name, type_name in zip(names, types, strict=True)
        ]
        return ResultSet(columns=columns, rows=rows, truncated=truncated, bytes_scanned=None)

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        """Observe a relation's columns from ``system.columns``, scanning no rows.

        ``relation`` is ``database.table`` or a bare ``table`` in the connection's default
        database. A relation that does not exist raises ``ValueError``.
        """
        database, name = _split_relation(relation)

        def _describe(client: Any) -> list[tuple[Any, ...]]:
            return list(
                client.query(
                    "SELECT name, type, position FROM system.columns "
                    "WHERE database = coalesce(nullIf({db:String}, ''), currentDatabase()) "
                    "AND table = {tbl:String} ORDER BY position",
                    parameters={"db": database, "tbl": name},
                ).result_rows
            )

        fetched = await self._in_session(_describe)
        if not fetched:
            raise ValueError(f"relation not found: {relation!r}")
        return [
            ColumnInfo(
                name=column,
                type=_column_type(type_name, relation, column),
                nullable=_unwrap_type(type_name)[1],
                position=int(position),
            )
            for column, type_name, position in fetched
        ]


def _system_databases() -> str:
    return "(" + ", ".join(f"'{name}'" for name in _SYSTEM_DATABASES) + ")"


def _fetch_relations(client: Any) -> dict[tuple[str, str], tuple[str, int | None]]:
    # total_rows is an estimate for MergeTree tables and NULL for views.
    rows = client.query(
        "SELECT database, name, engine, total_rows FROM system.tables "
        f"WHERE database NOT IN {_system_databases()} AND NOT is_temporary"
    ).result_rows
    return {
        (database, name): (
            "view" if engine in _VIEW_ENGINES else "table",
            int(total_rows) if total_rows is not None else None,
        )
        for database, name, engine, total_rows in rows
    }


def _fetch_columns(client: Any) -> dict[tuple[str, str], list[ColumnInfo]]:
    rows = client.query(
        "SELECT database, table, name, type, position FROM system.columns "
        f"WHERE database NOT IN {_system_databases()} "
        "ORDER BY database, table, position"
    ).result_rows
    out: dict[tuple[str, str], list[ColumnInfo]] = {}
    for database, table, column, type_name, position in rows:
        out.setdefault((database, table), []).append(
            ColumnInfo(
                name=column,
                type=_column_type(type_name, f"{database}.{table}", column),
                nullable=_unwrap_type(type_name)[1],
                position=int(position),
            )
        )
    return out
