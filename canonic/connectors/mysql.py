"""MySQL connector (queryable, MySQL 8.0 or newer).

Implements ``capabilities``, ``test_connection``, ``introspect_schema`` (live, tier 1) and
``run_read_only_sql`` with native read-only enforcement. In MySQL a "schema" is a database, so
relations are named ``database.table``. ``params["schemas"]``/``params["tables"]`` narrow the
relations that introspection returns (see ``canonic.connectors.relation_filter``).

The driver is PyMySQL, which is synchronous, so every call runs on a worker thread through
``asyncio.to_thread``, the same way the Snowflake and Databricks connectors do. It is an optional
dependency, imported when a connection is opened, with an install hint if it is missing. An async
driver was tried first and dropped: ``asyncmy`` never delivers the server's statement timeout on a
streamed result, so the query hangs.

Params: ``host``, ``port`` (default 3306), ``user``, ``database`` (optional, ``dbname`` is an
alias), ``schemas``, ``tables``, ``row_limit``, ``statement_timeout_ms`` and ``ssl`` (bool, verifies
the server certificate against the system trust store). The password comes from ``credentials_ref``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import ssl
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    ConnectorBase,
    ForeignKey,
    ForeignKeyRef,
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

__all__ = ["MySQLConnector"]

_DEFAULT_PORT = 3306
_DEFAULT_ROW_LIMIT = 10_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
_CONNECT_TIMEOUT_S = 10
# Socket read timeout beyond the statement timeout, so a server that never answers raises
# instead of blocking a worker thread for good. The server enforces the statement timeout itself.
_READ_TIMEOUT_GRACE_S = 10
_MIN_MAJOR_VERSION = 8

_SYSTEM_SCHEMAS = "('mysql', 'information_schema', 'performance_schema', 'sys')"

_KIND_BY_TABLE_TYPE = {"BASE TABLE": "table", "VIEW": "view"}

# information_schema.columns.data_type (lower-cased) -> normalized type set. ``tinyint(1)`` is
# MySQL's boolean and is resolved from column_type before this map is consulted.
_MYSQL_TYPE_MAP: dict[str, str] = {
    "tinyint": "int",
    "smallint": "int",
    "mediumint": "int",
    "int": "int",
    "integer": "int",
    "bigint": "int",
    "year": "int",
    "bit": "int",
    "decimal": "decimal",
    "numeric": "decimal",
    "float": "float",
    "double": "float",
    "real": "float",
    "char": "string",
    "varchar": "string",
    "tinytext": "string",
    "text": "string",
    "mediumtext": "string",
    "longtext": "string",
    "enum": "string",
    "set": "string",
    "time": "string",
    "date": "date",
    "datetime": "timestamp",
    "timestamp": "timestamp",
    "json": "json",
}


def _require_driver() -> Any:
    """Import the MySQL driver, translating its absence into an actionable error."""
    try:
        return importlib.import_module("pymysql")
    except ImportError as exc:
        raise ConnectionError(
            "connection type 'mysql' requires PyMySQL, which is not installed. "
            "Install it with: pip install 'canonic[mysql]' (or pip install pymysql)"
        ) from exc


def _as_text(value: Any) -> str:
    """Decode a metadata value, since MySQL may return string columns as bytes."""
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _fetch_all(con: Any, sql: str, params: tuple[Any, ...] | None = None) -> list[tuple[Any, ...]]:
    """Run ``sql`` on ``con`` and return every row."""
    cursor = con.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _normalize_type(data_type: str, column_type: str, relation: str, column: str) -> str:
    """Map a MySQL column type to the normalized type set.

    ``data_type`` is the bare name (``tinyint``) and ``column_type`` the full declaration
    (``tinyint(1)``). Unmappable types (blobs, spatial types) are recorded as ``json`` with a
    warning, never dropped silently.
    """
    base = data_type.strip().lower()
    if base == "tinyint" and column_type.strip().lower().startswith("tinyint(1)"):
        return "bool"
    mapped = _MYSQL_TYPE_MAP.get(base)
    if mapped is None:
        logger.warning(
            "unmapped MySQL type %r on %s.%s recorded as json", data_type, relation, column
        )
        return "json"
    return mapped


def _normalize_value_type(value: Any) -> str:
    """Best-effort normalized type name for a result value."""
    if isinstance(value, bool):  # bool is a subclass of int, so check it first
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


def _major_version(version: str) -> int | None:
    """Leading integer of a ``SELECT VERSION()`` string such as ``8.4.2`` or ``5.7.44-log``."""
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else None


class MySQLConnector(ConnectorBase):
    """Primary (queryable) connector for MySQL."""

    def __init__(self, connection: Connection) -> None:
        params = connection.params
        self._host = params.get("host")
        self._port = int(params.get("port", _DEFAULT_PORT))
        self._user = params.get("user")
        self._database = params.get("database") or params.get("dbname")
        self._password = resolve_credential(connection.credentials_ref)
        self._use_ssl = bool(params.get("ssl", False))
        self._schemas_filter: list[str] | None = params.get("schemas")
        self._tables_filter: list[str] | None = params.get("tables")
        self._row_limit = int(params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._statement_timeout_ms = int(
            params.get("statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS)
        )
        if params.get("fetch_column_stats"):
            logger.warning("fetch_column_stats is not supported for MySQL and is ignored")
        self._connection_id = connection.id

    def _connect(self) -> Any:
        """Open a session. Blocking, call from a worker thread."""
        pymysql = _require_driver()
        return pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=_CONNECT_TIMEOUT_S,
            read_timeout=self._statement_timeout_ms / 1000 + _READ_TIMEOUT_GRACE_S,
            ssl=ssl.create_default_context() if self._use_ssl else None,
        )

    async def _in_session[T](self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(connection)`` on a worker thread with a freshly opened session."""

        def _run() -> T:
            con = self._connect()
            try:
                return fn(con)
            finally:
                try:
                    con.close()
                except Exception:  # the connection may already be gone after a failed query
                    logger.debug("closing the MySQL connection failed", exc_info=True)

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
            version = _as_text((await self._in_session(_server_version))[0][0])
        except Exception as exc:  # by contract test_connection reports, never raises
            return Health(status="error", message=str(exc))
        major = _major_version(version)
        if major is not None and major < _MIN_MAJOR_VERSION and "mariadb" not in version.lower():
            return Health(
                status="error",
                message=(
                    f"MySQL {_MIN_MAJOR_VERSION}.0 or newer is required, server reports {version}"
                ),
            )
        return Health(status="ok")

    def read_only_enforcement(self) -> ReadOnlyEnforcement:
        return ReadOnlyEnforcement.NATIVE

    async def introspect_schema(self) -> list[RelationSchema]:
        return await self._in_session(self._introspect_sync)

    def _introspect_sync(self, con: Any) -> list[RelationSchema]:
        relations = _fetch_relations(con)
        relations = filter_relations(relations, self._schemas_filter, self._tables_filter)
        columns = _fetch_columns(con)
        primary_keys = _fetch_primary_keys(con)
        foreign_keys = _fetch_foreign_keys(con)
        row_estimates = _fetch_row_estimates(con)

        schemas: list[RelationSchema] = []
        for (schema, name), kind in sorted(relations.items()):
            cols = columns.get((schema, name), [])
            if not cols:
                continue
            pk = primary_keys.get((schema, name), [])
            fks = foreign_keys.get((schema, name), [])
            schemas.append(
                RelationSchema(
                    connection=self._connection_id,
                    relation=f"{schema}.{name}",
                    kind=kind,  # type: ignore[arg-type]
                    columns=cols,
                    primary_key=pk,
                    foreign_keys=fks,
                    row_count_estimate=row_estimates.get((schema, name)),
                    acquisition_tier=AcquisitionTier.LIVE,
                    source_fingerprint=compute_fingerprint(cols, pk, fks),
                )
            )
        return schemas

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        assert_read_only(sql, dialect="mysql")
        return await self._in_session(lambda con: self._run_sql_sync(con, sql))

    def _run_sql_sync(self, con: Any, sql: str) -> ResultSet:
        setup = con.cursor()
        try:
            setup.execute("SET SESSION TRANSACTION READ ONLY")
            setup.execute(f"SET SESSION max_execution_time = {self._statement_timeout_ms}")
        finally:
            setup.close()

        # Unbuffered, so a huge result is never held in memory beyond ``row_limit`` rows. Closing
        # an unfinished unbuffered cursor reads the rest of the result, so a truncated cursor is
        # left open and the connection close that follows makes the server abort the statement.
        cursor = con.cursor(_require_driver().cursors.SSCursor)
        cursor.execute(sql)
        description = cursor.description
        fetched: list[Any] = list(cursor.fetchmany(self._row_limit + 1))
        if not description:
            return ResultSet(columns=[], rows=[], truncated=False)
        truncated = len(fetched) > self._row_limit
        if not truncated:
            cursor.close()
        rows = [list(row) for row in fetched[: self._row_limit]]
        keys = [desc[0] for desc in description]
        return ResultSet(
            columns=self._result_columns(keys, rows),
            rows=rows,
            truncated=truncated,
            bytes_scanned=None,
        )

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        """Observe a relation's columns from ``information_schema``, scanning no rows.

        ``relation`` is ``database.table`` or a bare ``table`` in the connection's default
        database. A relation that does not exist raises ``ValueError``.
        """
        schema, _, name = relation.rpartition(".")
        if not name or "." in schema:
            raise ValueError(f"unsafe relation identifier: {relation!r}")

        def _describe(con: Any) -> list[tuple[Any, ...]]:
            return _fetch_all(
                con,
                "SELECT column_name, data_type, column_type, is_nullable, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema or None, name),
            )

        fetched = await self._in_session(_describe)
        if not fetched:
            raise ValueError(f"relation not found: {relation!r}")
        return [
            ColumnInfo(
                name=_as_text(column),
                type=_normalize_type(
                    _as_text(data_type), _as_text(column_type), relation, _as_text(column)
                ),
                nullable=(_as_text(is_nullable) == "YES"),
                position=int(position),
            )
            for column, data_type, column_type, is_nullable, position in fetched
        ]

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


def _server_version(con: Any) -> list[tuple[Any, ...]]:
    return _fetch_all(con, "SELECT VERSION()")


def _fetch_relations(con: Any) -> dict[tuple[str, str], str]:
    rows = _fetch_all(
        con,
        "SELECT table_schema, table_name, table_type "
        "FROM information_schema.tables "
        f"WHERE table_schema NOT IN {_SYSTEM_SCHEMAS}",
    )
    relations: dict[tuple[str, str], str] = {}
    for schema, name, table_type in rows:
        kind = _KIND_BY_TABLE_TYPE.get(_as_text(table_type))
        if kind is not None:
            relations[(_as_text(schema), _as_text(name))] = kind
    return relations


def _fetch_columns(con: Any) -> dict[tuple[str, str], list[ColumnInfo]]:
    rows = _fetch_all(
        con,
        "SELECT table_schema, table_name, column_name, data_type, column_type, "
        "is_nullable, ordinal_position "
        "FROM information_schema.columns "
        f"WHERE table_schema NOT IN {_SYSTEM_SCHEMAS} "
        "ORDER BY table_schema, table_name, ordinal_position",
    )
    out: dict[tuple[str, str], list[ColumnInfo]] = {}
    for schema, name, column, data_type, column_type, is_nullable, position in rows:
        schema, name, column = _as_text(schema), _as_text(name), _as_text(column)
        out.setdefault((schema, name), []).append(
            ColumnInfo(
                name=column,
                type=_normalize_type(
                    _as_text(data_type), _as_text(column_type), f"{schema}.{name}", column
                ),
                nullable=(_as_text(is_nullable) == "YES"),
                position=int(position),
            )
        )
    return out


def _fetch_primary_keys(con: Any) -> dict[tuple[str, str], list[str]]:
    # The primary key constraint of a MySQL table is always named PRIMARY.
    rows = _fetch_all(
        con,
        "SELECT table_schema, table_name, column_name "
        "FROM information_schema.key_column_usage "
        "WHERE constraint_name = 'PRIMARY' "
        f"AND table_schema NOT IN {_SYSTEM_SCHEMAS} "
        "ORDER BY table_schema, table_name, ordinal_position",
    )
    out: dict[tuple[str, str], list[str]] = {}
    for schema, name, column in rows:
        out.setdefault((_as_text(schema), _as_text(name)), []).append(_as_text(column))
    return out


def _fetch_foreign_keys(con: Any) -> dict[tuple[str, str], list[ForeignKey]]:
    rows = _fetch_all(
        con,
        "SELECT table_schema, table_name, constraint_name, column_name, "
        "referenced_table_schema, referenced_table_name, referenced_column_name "
        "FROM information_schema.key_column_usage "
        "WHERE referenced_table_name IS NOT NULL "
        f"AND table_schema NOT IN {_SYSTEM_SCHEMAS} "
        "ORDER BY table_schema, table_name, constraint_name, ordinal_position",
    )
    # Group rows per (relation, constraint), preserving column order.
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for schema, name, constraint, column, ref_schema, ref_table, ref_column in rows:
        entry = grouped.setdefault(
            (_as_text(schema), _as_text(name), _as_text(constraint)),
            {
                "columns": [],
                "ref_relation": f"{_as_text(ref_schema)}.{_as_text(ref_table)}",
                "ref_columns": [],
            },
        )
        entry["columns"].append(_as_text(column))
        entry["ref_columns"].append(_as_text(ref_column))

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


def _fetch_row_estimates(con: Any) -> dict[tuple[str, str], int | None]:
    # table_rows is an InnoDB estimate, and NULL for views.
    rows = _fetch_all(
        con,
        "SELECT table_schema, table_name, table_rows "
        "FROM information_schema.tables "
        f"WHERE table_schema NOT IN {_SYSTEM_SCHEMAS}",
    )
    return {
        (_as_text(schema), _as_text(name)): int(count) if count is not None else None
        for schema, name, count in rows
    }
