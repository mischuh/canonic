"""Snowflake connector (SPEC-E2 §6, AMENDMENT-connector-factory).

Implements the four P0 capabilities against Snowflake: ``capabilities``,
``test_connection``, ``introspect_schema`` (live, tier 1) and ``run_read_only_sql``.

``snowflake-connector-python`` is synchronous, so every blocking call runs through
``asyncio.to_thread``, the same way the DuckDB connector does. The driver is an optional
dependency: it is imported when a connection is opened, with an install hint if it is
missing, so the module (and the connector factory) import on a checkout without it.

Authentication is either a password or a key pair. ``credentials_ref`` resolves to the
password, or to the private key passphrase when ``params.private_key_path`` is set. A
``provider:`` ref is refreshed before every connect, so expiring credentials work once a
provider is registered.

Snowflake has no read-only session flag. Read-only safety is the parse-level
``assert_read_only()`` guard plus the session role: ``Connection.read_only_role`` (or
``params.role``) should name a role that only holds ``SELECT`` grants.

Snowflake stores unquoted identifiers upper-cased and the compiler quotes every
identifier, so relation and column names in a semantic source must match the stored case.
Introspection returns names exactly as Snowflake stores them.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from canonic.connectors.base import (
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
from canonic.connectors.readonly import assert_read_only
from canonic.connectors.relation_filter import filter_relations
from canonic.credentials import CredentialSource, credential_source
from canonic.exc import ConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["SnowflakeConnector"]

_DEFAULT_ROW_LIMIT = 10_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
_QUERY_TAG = "canonic"

_SAFE_RELATION = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_$]*\.){0,2}[A-Za-z_][A-Za-z0-9_$]*$")
_NUMBER_SCALE = re.compile(r"\(\s*\d+\s*,\s*(\d+)\s*\)")

_KIND_BY_TABLE_TYPE = {
    "BASE TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
}

_INT_TYPES = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT"}
_DECIMAL_TYPES = {"NUMBER", "DECIMAL", "NUMERIC", "FIXED"}
_FLOAT_TYPES = {"FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"}
_STRING_TYPES = {"TEXT", "VARCHAR", "CHAR", "CHARACTER", "STRING", "NVARCHAR", "NCHAR"}
_TIMESTAMP_TYPES = {
    "TIMESTAMP",
    "TIMESTAMP_NTZ",
    "TIMESTAMP_LTZ",
    "TIMESTAMP_TZ",
    "DATETIME",
}
_JSON_TYPES = {
    "VARIANT",
    "OBJECT",
    "ARRAY",
    "BINARY",
    "VARBINARY",
    "GEOGRAPHY",
    "GEOMETRY",
    "VECTOR",
}


def _require_driver() -> Any:
    """Import the Snowflake driver, translating its absence into an actionable error."""
    try:
        return importlib.import_module("snowflake.connector")
    except ImportError as exc:
        raise ConnectionError(
            "connection type 'snowflake' requires snowflake-connector-python, which is not "
            "installed. Install it with: pip install 'canonic[snowflake]' "
            "(or pip install snowflake-connector-python)"
        ) from exc


def _quote_ident(name: str) -> str:
    """Quote a Snowflake identifier, escaping embedded double quotes."""
    return '"' + name.replace('"', '""') + '"'


def _normalize_type(raw: str, relation: str, column: str, scale: int | None = None) -> str:
    """Map a Snowflake data type name to the canonical normalized type set.

    ``NUMBER`` is an int when its scale is 0 and a decimal otherwise. The scale comes from
    ``scale`` (``INFORMATION_SCHEMA.COLUMNS.NUMERIC_SCALE``) or, failing that, from the
    ``NUMBER(p,s)`` form that ``DESCRIBE`` returns. Unmappable types fall back to ``json``
    with a warning, never dropped silently.
    """
    t = re.sub(r"\(.*\)", "", raw.strip().upper()).strip()
    if t in _INT_TYPES:
        return "int"
    if t in _DECIMAL_TYPES:
        if scale is None and (match := _NUMBER_SCALE.search(raw)) is not None:
            scale = int(match.group(1))
        return "int" if scale == 0 else "decimal"
    if t in _FLOAT_TYPES:
        return "float"
    if t in _STRING_TYPES:
        return "string"
    if t in {"BOOLEAN", "BOOL"}:
        return "bool"
    if t == "DATE":
        return "date"
    if t in _TIMESTAMP_TYPES:
        return "timestamp"
    if t == "TIME":
        return "string"
    if t in _JSON_TYPES:
        return "json"
    logger.warning("unmapped Snowflake type %r on %s.%s recorded as json", raw, relation, column)
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
    """Run a ``SHOW ... KEYS`` query, degrading to no rows when the driver rejects it.

    Declared keys are informational in Snowflake (never enforced) and only enrich join
    inference, so a role that cannot see them must not fail the whole introspection.
    """
    try:
        return _fetch_dicts(con, sql)
    except _require_driver().Error as exc:
        logger.warning("could not read Snowflake %s, introspecting without them: %s", what, exc)
        return []


class SnowflakeConnector(ConnectorBase):
    """Primary (queryable) connector for Snowflake.

    Params: ``account``, ``user`` (required), ``database`` (required for introspection),
    ``warehouse``, ``schema``, ``role``, ``private_key_path``, plus the shared
    ``schemas``, ``tables``, ``row_limit`` and ``statement_timeout_ms``.
    """

    def __init__(self, connection: Connection) -> None:
        params = connection.params
        missing = [key for key in ("account", "user") if not params.get(key)]
        if missing:
            raise ConnectionError(
                f"connection {connection.id!r} (type=snowflake) requires params: "
                + ", ".join(missing)
            )
        self._connection_id = connection.id
        self._account: str = params["account"]
        self._user: str = params["user"]
        self._warehouse: str | None = params.get("warehouse")
        self._database: str | None = params.get("database")
        self._schema: str | None = params.get("schema")
        self._role: str | None = connection.read_only_role or params.get("role")
        self._private_key_path: str | None = params.get("private_key_path")
        self._schemas_filter: list[str] | None = params.get("schemas")
        self._tables_filter: list[str] | None = params.get("tables")
        self._row_limit = int(params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._statement_timeout_ms = int(
            params.get("statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS)
        )
        self._fetch_column_stats = bool(params.get("fetch_column_stats", False))
        # With key-pair auth the ref is only the optional key passphrase.
        self._credentials: CredentialSource | None = (
            None
            if self._private_key_path and connection.credentials_ref is None
            else credential_source(connection.credentials_ref, params=params)
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
        snowflake = _require_driver()
        secret = self._credentials.cached_value().value if self._credentials is not None else None
        kwargs: dict[str, Any] = {
            "account": self._account,
            "user": self._user,
            "session_parameters": {
                "STATEMENT_TIMEOUT_IN_SECONDS": math.ceil(self._statement_timeout_ms / 1000),
                "QUERY_TAG": _QUERY_TAG,
            },
        }
        for key, value in (
            ("warehouse", self._warehouse),
            ("database", self._database),
            ("schema", self._schema),
            ("role", self._role),
        ):
            if value:
                kwargs[key] = value
        if self._private_key_path:
            kwargs["private_key"] = self._load_private_key(secret)
        else:
            kwargs["password"] = secret
        return snowflake.connect(**kwargs)

    def _load_private_key(self, passphrase: str | None) -> bytes:
        """Load the PEM key at ``params.private_key_path`` as the DER bytes the driver wants."""
        serialization = importlib.import_module("cryptography.hazmat.primitives.serialization")
        assert self._private_key_path is not None
        pem = Path(self._private_key_path).expanduser().read_bytes()
        key = serialization.load_pem_private_key(
            pem, password=passphrase.encode() if passphrase else None
        )
        der: bytes = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return der

    async def _in_session[T](self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(connection)`` on a worker thread with a freshly opened session.

        A provider-backed credential is refreshed first, off the event loop, so a connect
        never reuses an expired one.
        """
        if self._credentials is not None:
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
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        if self._database is None:
            raise ConnectionError(
                f"connection {self._connection_id!r} (type=snowflake) requires params.database "
                "for schema introspection"
            )
        if self._fetch_column_stats:
            logger.warning(
                "fetch_column_stats=True requested on Snowflake connection %r, but Snowflake has "
                "no planner statistics to read without a scan; ignoring (stats omitted)",
                self._connection_id,
            )
        return await self._in_session(self._introspect_sync)

    def _introspect_sync(self, con: Any) -> list[RelationSchema]:
        assert self._database is not None
        catalog = f"{_quote_ident(self._database)}.INFORMATION_SCHEMA"
        relations, row_estimates = self._fetch_relations(con, catalog)
        relations = filter_relations(relations, self._schemas_filter, self._tables_filter)
        columns = self._fetch_columns(con, catalog)
        primary_keys = self._fetch_primary_keys(con)
        foreign_keys = self._fetch_foreign_keys(con)

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

    @staticmethod
    def _fetch_relations(
        con: Any, catalog: str
    ) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], int | None]]:
        rows = _fetch_dicts(
            con,
            "SELECT table_schema, table_name, table_type, row_count "
            f"FROM {catalog}.TABLES WHERE table_schema <> 'INFORMATION_SCHEMA'",
        )
        relations: dict[tuple[str, str], str] = {}
        row_estimates: dict[tuple[str, str], int | None] = {}
        for row in rows:
            kind = _KIND_BY_TABLE_TYPE.get(row["table_type"])
            if kind is None:
                continue
            key = (row["table_schema"], row["table_name"])
            relations[key] = kind
            row_estimates[key] = int(row["row_count"]) if row["row_count"] is not None else None
        return relations, row_estimates

    @staticmethod
    def _fetch_columns(con: Any, catalog: str) -> dict[tuple[str, str], list[ColumnInfo]]:
        rows = _fetch_dicts(
            con,
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, "
            "ordinal_position, numeric_scale "
            f"FROM {catalog}.COLUMNS WHERE table_schema <> 'INFORMATION_SCHEMA' "
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

    def _fetch_primary_keys(self, con: Any) -> dict[tuple[str, str], list[str]]:
        """Primary keys via ``SHOW PRIMARY KEYS``. Informational in Snowflake (not enforced)."""
        assert self._database is not None
        rows = _fetch_key_rows(
            con, f"SHOW PRIMARY KEYS IN DATABASE {_quote_ident(self._database)}", "primary keys"
        )
        grouped: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for row in rows:
            grouped.setdefault((row["schema_name"], row["table_name"]), []).append(
                (int(row["key_sequence"]), row["column_name"])
            )
        return {key: [col for _, col in sorted(cols)] for key, cols in grouped.items()}

    def _fetch_foreign_keys(self, con: Any) -> dict[tuple[str, str], list[ForeignKey]]:
        """Foreign keys via ``SHOW IMPORTED KEYS``. Informational in Snowflake (not enforced)."""
        assert self._database is not None
        rows = _fetch_key_rows(
            con, f"SHOW IMPORTED KEYS IN DATABASE {_quote_ident(self._database)}", "foreign keys"
        )
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (row["fk_schema_name"], row["fk_table_name"], row["fk_name"])
            grouped.setdefault(key, []).append(row)

        out: dict[tuple[str, str], list[ForeignKey]] = {}
        for (schema, name, _constraint), parts in grouped.items():
            parts.sort(key=lambda r: int(r["key_sequence"]))
            first = parts[0]
            out.setdefault((schema, name), []).append(
                ForeignKey(
                    columns=[p["fk_column_name"] for p in parts],
                    references=ForeignKeyRef(
                        relation=f"{first['pk_schema_name']}.{first['pk_table_name']}",
                        columns=[p["pk_column_name"] for p in parts],
                    ),
                )
            )
        return out

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        if not _SAFE_RELATION.match(relation):
            raise ValueError(f"unsafe relation identifier: {relation!r}")

        def _describe(con: Any) -> list[ColumnInfo]:
            rows = _fetch_dicts(con, f"DESC TABLE {relation}")
            return [
                ColumnInfo(
                    name=row["name"],
                    type=_normalize_type(row["type"], relation, row["name"]),
                    nullable=row["null?"] == "Y",
                    position=i + 1,
                )
                for i, row in enumerate(rows)
            ]

        return await self._in_session(_describe)

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        assert_read_only(sql, dialect="snowflake")
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
