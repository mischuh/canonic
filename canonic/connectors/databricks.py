"""Databricks connector (SPEC-E2 §6, AMENDMENT-connector-factory).

Implements the four P0 capabilities against a Databricks SQL warehouse: ``capabilities``,
``test_connection``, ``introspect_schema`` (live, tier 1, from Unity Catalog's
``information_schema``) and ``run_read_only_sql``.

``databricks-sql-connector`` is synchronous, so every blocking call runs through
``asyncio.to_thread``, the same way the Snowflake connector does. The driver is an optional
dependency: it is imported when a connection is opened, with an install hint if it is
missing, so the module (and the connector factory) import on a checkout without it.

Authentication is a personal access token. ``credentials_ref`` resolves to the token, and a
``provider:`` ref is refreshed before every connect, so expiring tokens work once a provider
is registered.

Databricks has no read-only session flag and no role to switch to, the session runs as the
authenticated principal. Read-only safety is the parse-level ``assert_read_only()`` guard plus
the grants of that principal, which should be limited to ``USE CATALOG``, ``USE SCHEMA`` and
``SELECT``. The connector therefore reports ``PARSE_ONLY`` enforcement, like Redshift.

Unity Catalog identifiers are case-insensitive and stored lower-case. Introspection returns
names exactly as Unity Catalog stores them.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import re
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
from canonic.credentials import CredentialSource, credential_source
from canonic.exc import ConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["DatabricksConnector"]

_DEFAULT_ROW_LIMIT = 10_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
_USER_AGENT = "canonic"

_SAFE_RELATION = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.){0,2}[A-Za-z_][A-Za-z0-9_]*$")
_PARAMETERS = re.compile(r"\(.*\)|<.*>")
_DECIMAL_SCALE = re.compile(r"\(\s*\d+\s*,\s*(\d+)\s*\)")

_KIND_BY_TABLE_TYPE = {
    "VIEW": "view",
    "MATERIALIZED_VIEW": "materialized_view",
}

# information_schema.columns.data_type uses the Spark catalog names (BYTE, SHORT, LONG),
# DESCRIBE TABLE uses the SQL names (TINYINT, SMALLINT, BIGINT). Both are accepted.
_INT_TYPES = {"BYTE", "TINYINT", "SHORT", "SMALLINT", "INT", "INTEGER", "LONG", "BIGINT"}
_DECIMAL_TYPES = {"DECIMAL", "DEC", "NUMERIC"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL"}
_STRING_TYPES = {"STRING", "VARCHAR", "CHAR", "CHARACTER"}
_TIMESTAMP_TYPES = {"TIMESTAMP", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ"}
_JSON_TYPES = {"ARRAY", "MAP", "STRUCT", "VARIANT", "BINARY", "OBJECT", "INTERVAL"}


def _require_driver() -> Any:
    """Import the Databricks driver, translating its absence into an actionable error."""
    try:
        return importlib.import_module("databricks.sql")
    except ImportError as exc:
        raise ConnectionError(
            "connection type 'databricks' requires databricks-sql-connector, which is not "
            "installed. Install it with: pip install 'canonic[databricks]' "
            "(or pip install databricks-sql-connector)"
        ) from exc


def _quote_ident(name: str) -> str:
    """Quote a Databricks identifier, escaping embedded backticks."""
    return "`" + name.replace("`", "``") + "`"


def _normalize_type(raw: str, relation: str, column: str, scale: int | None = None) -> str:
    """Map a Databricks data type name to the canonical normalized type set.

    ``DECIMAL`` is an int when its scale is 0 and a decimal otherwise. The scale comes from
    ``scale`` (``information_schema.columns.numeric_scale``) or, failing that, from the
    ``DECIMAL(p,s)`` form that ``DESCRIBE TABLE`` returns. Unmappable types fall back to
    ``json`` with a warning, never dropped silently.
    """
    t = _PARAMETERS.sub("", raw.strip().upper()).strip()
    if t in _INT_TYPES:
        return "int"
    if t in _DECIMAL_TYPES:
        if scale is None and (match := _DECIMAL_SCALE.search(raw)) is not None:
            scale = int(match.group(1))
        return "int" if scale == 0 else "decimal"
    if t in _FLOAT_TYPES:
        return "float"
    if t in _STRING_TYPES:
        return "string"
    if t == "BOOLEAN":
        return "bool"
    if t == "DATE":
        return "date"
    if t in _TIMESTAMP_TYPES:
        return "timestamp"
    if t in _JSON_TYPES:
        return "json"
    logger.warning("unmapped Databricks type %r on %s.%s recorded as json", raw, relation, column)
    return "json"


def _normalize_value_type(value: Any) -> str:
    """Best-effort normalized type name for a result value."""
    if isinstance(value, bool):  # bool is a subclass of int, check first
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


def _fetch_dicts(con: Any, sql: str) -> list[dict[str, Any]]:
    """Run ``sql`` and return rows as dicts keyed by lower-cased column name."""
    cursor = con.cursor()
    try:
        cursor.execute(sql)
        names = [desc[0].lower() for desc in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        cursor.close()


def _fetch_key_rows(con: Any, sql: str, what: str) -> list[dict[str, Any]]:
    """Run a constraint query, degrading to no rows when the driver rejects it.

    Declared keys are informational in Databricks (never enforced) and only enrich join
    inference, so a principal that cannot see them must not fail the whole introspection.
    """
    try:
        return _fetch_dicts(con, sql)
    except _require_driver().Error as exc:
        logger.warning("could not read Databricks %s, introspecting without them: %s", what, exc)
        return []


class DatabricksConnector(ConnectorBase):
    """Primary (queryable) connector for a Databricks SQL warehouse.

    Params: ``server_hostname``, ``http_path`` (required), ``catalog`` (required for
    introspection), ``schema``, plus the shared ``schemas``, ``tables``, ``row_limit`` and
    ``statement_timeout_ms``. ``credentials_ref`` resolves to the access token.
    """

    def __init__(self, connection: Connection) -> None:
        params = connection.params
        missing = [key for key in ("server_hostname", "http_path") if not params.get(key)]
        if missing:
            raise ConnectionError(
                f"connection {connection.id!r} (type=databricks) requires params: "
                + ", ".join(missing)
            )
        self._connection_id = connection.id
        self._server_hostname: str = params["server_hostname"]
        self._http_path: str = params["http_path"]
        self._catalog: str | None = params.get("catalog")
        self._schema: str | None = params.get("schema")
        self._schemas_filter: list[str] | None = params.get("schemas")
        self._tables_filter: list[str] | None = params.get("tables")
        self._row_limit = int(params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._statement_timeout_ms = int(
            params.get("statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS)
        )
        self._fetch_column_stats = bool(params.get("fetch_column_stats", False))
        self._credentials: CredentialSource = credential_source(
            connection.credentials_ref, params=params
        )

    def capabilities(self) -> list[Capability]:
        return [
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        ]

    def _connect(self) -> Any:
        """Open a session. Blocking, call from a worker thread."""
        databricks = _require_driver()
        kwargs: dict[str, Any] = {
            "server_hostname": self._server_hostname,
            "http_path": self._http_path,
            "access_token": self._credentials.cached_value().value,
            "session_configuration": {
                "STATEMENT_TIMEOUT": str(math.ceil(self._statement_timeout_ms / 1000)),
            },
            "user_agent_entry": _USER_AGENT,
        }
        for key, value in (("catalog", self._catalog), ("schema", self._schema)):
            if value:
                kwargs[key] = value
        return databricks.connect(**kwargs)

    async def _in_session[T](self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(connection)`` on a worker thread with a freshly opened session.

        A provider-backed credential is refreshed first, off the event loop, so a connect
        never reuses an expired one.
        """
        await self._credentials.arefresh()

        def _run() -> T:
            con = self._connect()
            try:
                return fn(con)
            finally:
                con.close()

        return await asyncio.to_thread(_run)

    async def test_connection(self) -> Health:
        def _check(con: Any) -> None:
            cursor = con.cursor()
            try:
                cursor.execute("SELECT 1")
            finally:
                cursor.close()

        try:
            await self._in_session(_check)
        except Exception as exc:  # by contract test_connection reports, never raises
            return Health(status="error", message=str(exc))
        return Health(status="ok", warnings=self.read_only_warnings())

    def read_only_enforcement(self) -> ReadOnlyEnforcement:
        return ReadOnlyEnforcement.PARSE_ONLY

    async def introspect_schema(self) -> list[RelationSchema]:
        if self._catalog is None:
            raise ConnectionError(
                f"connection {self._connection_id!r} (type=databricks) requires params.catalog "
                "for schema introspection"
            )
        if self._fetch_column_stats:
            logger.warning(
                "fetch_column_stats=True requested on Databricks connection %r, but Databricks "
                "has no planner statistics to read without a scan; ignoring (stats omitted)",
                self._connection_id,
            )
        return await self._in_session(self._introspect_sync)

    def _introspect_sync(self, con: Any) -> list[RelationSchema]:
        assert self._catalog is not None
        catalog = f"{_quote_ident(self._catalog)}.information_schema"
        relations = self._fetch_relations(con, catalog)
        relations = filter_relations(relations, self._schemas_filter, self._tables_filter)
        columns = self._fetch_columns(con, catalog)
        constraints = _fetch_key_rows(
            con,
            "SELECT kcu.constraint_schema, kcu.constraint_name, tc.constraint_type, "
            "kcu.table_schema, kcu.table_name, kcu.column_name, kcu.ordinal_position "
            f"FROM {catalog}.key_column_usage kcu "
            f"JOIN {catalog}.table_constraints tc "
            "ON kcu.constraint_catalog = tc.constraint_catalog "
            "AND kcu.constraint_schema = tc.constraint_schema "
            "AND kcu.constraint_name = tc.constraint_name "
            "ORDER BY kcu.constraint_schema, kcu.constraint_name, kcu.ordinal_position",
            "key constraints",
        )
        references = _fetch_key_rows(
            con,
            "SELECT constraint_schema, constraint_name, unique_constraint_schema, "
            f"unique_constraint_name FROM {catalog}.referential_constraints",
            "foreign key references",
        )
        primary_keys = self._primary_keys(constraints)
        foreign_keys = self._foreign_keys(constraints, references)

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
                    row_count_estimate=None,
                    acquisition_tier=AcquisitionTier.LIVE,
                    source_fingerprint=compute_fingerprint(cols, pk, fks),
                )
            )
        return schemas

    @staticmethod
    def _fetch_relations(con: Any, catalog: str) -> dict[tuple[str, str], str]:
        rows = _fetch_dicts(
            con,
            "SELECT table_schema, table_name, table_type "
            f"FROM {catalog}.tables WHERE table_schema <> 'information_schema'",
        )
        return {
            (row["table_schema"], row["table_name"]): _KIND_BY_TABLE_TYPE.get(
                row["table_type"], "table"
            )
            for row in rows
        }

    @staticmethod
    def _fetch_columns(con: Any, catalog: str) -> dict[tuple[str, str], list[ColumnInfo]]:
        rows = _fetch_dicts(
            con,
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, "
            "ordinal_position, numeric_scale "
            f"FROM {catalog}.columns WHERE table_schema <> 'information_schema' "
            "ORDER BY table_schema, table_name, ordinal_position",
        )
        out: dict[tuple[str, str], list[ColumnInfo]] = {}
        for row in rows:
            schema, name, column = row["table_schema"], row["table_name"], row["column_name"]
            scale = row["numeric_scale"]
            out.setdefault((schema, name), []).append(
                ColumnInfo(
                    name=column,
                    type=_normalize_type(
                        row["data_type"],
                        f"{schema}.{name}",
                        column,
                        scale=int(scale) if scale is not None else None,
                    ),
                    nullable=row["is_nullable"] == "YES",
                    position=int(row["ordinal_position"]),
                )
            )
        return out

    @staticmethod
    def _primary_keys(constraints: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
        """Primary keys from the constraint rows. Informational in Databricks (not enforced)."""
        out: dict[tuple[str, str], list[str]] = {}
        for row in constraints:
            if row["constraint_type"] == "PRIMARY KEY":
                out.setdefault((row["table_schema"], row["table_name"]), []).append(
                    row["column_name"]
                )
        return out

    @staticmethod
    def _foreign_keys(
        constraints: list[dict[str, Any]], references: list[dict[str, Any]]
    ) -> dict[tuple[str, str], list[ForeignKey]]:
        """Foreign keys from the constraint rows. Informational in Databricks (not enforced).

        The referenced columns are the key columns of the unique constraint that
        ``referential_constraints`` names, matched to the foreign key columns by position.
        """
        by_constraint: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in constraints:
            by_constraint.setdefault((row["constraint_schema"], row["constraint_name"]), []).append(
                row
            )
        targets = {
            (ref["constraint_schema"], ref["constraint_name"]): (
                ref["unique_constraint_schema"],
                ref["unique_constraint_name"],
            )
            for ref in references
        }

        out: dict[tuple[str, str], list[ForeignKey]] = {}
        for key, parts in by_constraint.items():
            first = parts[0]
            target = by_constraint.get(targets.get(key, ("", "")))
            if first["constraint_type"] != "FOREIGN KEY" or not target:
                continue
            if len(target) != len(parts):
                continue
            out.setdefault((first["table_schema"], first["table_name"]), []).append(
                ForeignKey(
                    columns=[p["column_name"] for p in parts],
                    references=ForeignKeyRef(
                        relation=f"{target[0]['table_schema']}.{target[0]['table_name']}",
                        columns=[t["column_name"] for t in target],
                    ),
                )
            )
        return out

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        if not _SAFE_RELATION.match(relation):
            raise ValueError(f"unsafe relation identifier: {relation!r}")

        def _describe(con: Any) -> list[ColumnInfo]:
            rows = _fetch_dicts(con, f"DESCRIBE TABLE {relation}")
            columns: list[ColumnInfo] = []
            for row in rows:
                name = row["col_name"]
                # DESCRIBE TABLE appends partition and metadata sections after a blank row.
                if not name or name.startswith("#"):
                    break
                columns.append(
                    ColumnInfo(
                        name=name,
                        type=_normalize_type(row["data_type"], relation, name),
                        nullable=True,
                        position=len(columns) + 1,
                    )
                )
            return columns

        return await self._in_session(_describe)

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        assert_read_only(sql, dialect="databricks")
        return await self._in_session(lambda con: self._run_sql_sync(con, sql))

    def _run_sql_sync(self, con: Any, sql: str) -> ResultSet:
        cursor = con.cursor()
        try:
            cursor.execute(sql)
            description = cursor.description
            fetched: list[Any] = cursor.fetchmany(self._row_limit + 1)
        finally:
            cursor.close()

        if not description:
            return ResultSet(columns=[], rows=[], truncated=False)

        truncated = len(fetched) > self._row_limit
        rows = [list(row) for row in fetched[: self._row_limit]]
        columns = _result_columns([desc[0] for desc in description], rows)
        return ResultSet(columns=columns, rows=rows, truncated=truncated, bytes_scanned=None)
