"""Amazon Redshift connector (SPEC-E2 §6).

Implements the four P0 capabilities against Redshift: ``capabilities``,
``test_connection``, ``introspect_schema`` (live, tier 1) and
``run_read_only_sql``.

Redshift is wire-protocol-compatible with PostgreSQL so the asyncpg driver is
used unchanged.  Key behavioral differences from the PostgreSQL connector:
- Default port 5439.
- ``default_transaction_read_only`` is not a Redshift SET parameter; read-only
  safety relies on the parse-level ``assert_read_only()`` guard.
- Materialized views are discovered via ``SVV_MV_INFO`` (not ``pg_matviews``).
- Redshift-specific types (SUPER, HLLSKETCH, VARBYTE, …) are mapped to json.
- System schema exclusion includes ``pg_internal``.

``introspect_schema`` honors ``params["schemas"]``/``params["tables"]`` to narrow
the relations it returns (see ``canonic.connectors.relation_filter``), and
``params["fetch_column_stats"]`` (bool, default False) to additionally merge
zero-scan cardinality/null-ratio stats from ``pg_stats`` onto each column.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import URL, make_url, text
from sqlalchemy.dialects import registry as _dialect_registry
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
from canonic.credentials import resolve_credential

if TYPE_CHECKING:
    from canonic.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["RedshiftConnector"]


class _RedshiftAsyncpgDialect(PGDialect_asyncpg):
    """asyncpg dialect patched for Redshift wire-protocol compatibility.

    Redshift does not support ``SHOW standard_conforming_strings``; it always
    behaves as if the setting is ``on``.
    """

    name = "redshift"

    def _set_backslash_escapes(self, _connection: Any) -> None:
        self._backslash_escapes = False


_dialect_registry.register("redshift.asyncpg", __name__, "_RedshiftAsyncpgDialect")

_DEFAULT_ROW_LIMIT = 10_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
_DEFAULT_PORT = 5439

_SAFE_RELATION = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*$")

# Redshift native type (parameter- and array-stripped, lower-cased) → normalized
# type set: string, int, decimal, float, bool, date, timestamp, json.
_REDSHIFT_TYPE_MAP: dict[str, str] = {
    "smallint": "int",
    "int2": "int",
    "integer": "int",
    "int": "int",
    "int4": "int",
    "bigint": "int",
    "int8": "int",
    "numeric": "decimal",
    "decimal": "decimal",
    "real": "float",
    "float4": "float",
    "double precision": "float",
    "float": "float",
    "float8": "float",
    "boolean": "bool",
    "bool": "bool",
    "character": "string",
    "char": "string",
    "bpchar": "string",
    "character varying": "string",
    "varchar": "string",
    "nvarchar": "string",
    "text": "string",
    "name": "string",
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "time without time zone": "string",
    "time with time zone": "string",
    "timezoneoid": "string",
    # Redshift-specific types
    "super": "json",
    "hllsketch": "json",
    "geometry": "json",
    "geography": "json",
    "varbyte": "json",
}

_KIND_BY_TABLE_TYPE = {"BASE TABLE": "table", "VIEW": "view"}


def _resolve_search_path(params: dict[str, Any]) -> str | None:
    """Derive the connect-time search_path: ``schemas`` (list) over legacy ``schema``."""
    schemas = params.get("schemas")
    if schemas:
        return ",".join(schemas)
    return params.get("schema")


def _normalize_type(raw: str, relation: str, column: str) -> str:
    """Map a native Redshift type name to the normalized type set.

    Unmappable types are recorded as ``json`` with a warning, never dropped
    silently (SPEC-E2 §2.1, S2 AC2).
    """
    t = raw.strip().lower()
    if t.endswith("[]") or t == "array":
        logger.warning("array type %r on %s.%s recorded as json", raw, relation, column)
        return "json"
    t = re.sub(r"\(.*\)", "", t).strip()  # drop size/precision parameters
    mapped = _REDSHIFT_TYPE_MAP.get(t)
    if mapped is None:
        logger.warning("unmapped Redshift type %r on %s.%s recorded as json", raw, relation, column)
        return "json"
    return mapped


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


def _normalize_distinct_count(n_distinct: float | None, row_estimate: int | None) -> int | None:
    """Normalize pg_stats.n_distinct's dual-sign encoding into an absolute distinct-count estimate.

    A positive value is an absolute estimated distinct-value count; a negative value is
    -1 * (distinct fraction of rows), used when the planner expects cardinality to scale
    with table size (e.g. near-unique columns). A negative value with no row estimate
    available cannot be resolved to an absolute count and is dropped rather than guessed.
    """
    if n_distinct is None:
        return None
    if n_distinct >= 0:
        return int(round(n_distinct))
    if row_estimate is None or row_estimate <= 0:
        return None
    return int(round(-n_distinct * row_estimate))


def _apply_column_stats(
    cols: list[ColumnInfo],
    stats: dict[str, tuple[float | None, float | None]],
    row_estimate: int | None,
) -> list[ColumnInfo]:
    """Merge pg_stats-derived cardinality/null stats onto already-built ColumnInfo objects.

    ColumnInfo is frozen, so enrichment goes through model_copy rather than attribute
    assignment. A column absent from ``stats`` (never analyzed) is returned unchanged.
    """
    updated: list[ColumnInfo] = []
    for col in cols:
        entry = stats.get(col.name)
        if entry is None:
            updated.append(col)
            continue
        null_frac, n_distinct = entry
        distinct_count = _normalize_distinct_count(n_distinct, row_estimate)
        uniqueness_ratio = (
            distinct_count / row_estimate
            if distinct_count is not None and row_estimate is not None and row_estimate > 0
            else None
        )
        updated.append(
            col.model_copy(
                update={
                    "distinct_count_estimate": distinct_count,
                    "null_fraction": null_frac,
                    "uniqueness_ratio": uniqueness_ratio,
                    "stats_source": "pg_stats",
                }
            )
        )
    return updated


class RedshiftConnector(ConnectorBase):
    """Primary (queryable) connector for Amazon Redshift."""

    def __init__(self, connection: Connection) -> None:
        params = connection.params
        credential = resolve_credential(connection.credentials_ref)
        if credential.startswith(
            ("redshift://", "redshift+asyncpg://", "postgres://", "postgresql://")
        ):
            self._url = make_url(credential).set(drivername="redshift+asyncpg")
        else:
            self._url = URL.create(
                "redshift+asyncpg",
                username=params.get("user"),
                password=credential,
                host=params.get("host"),
                port=int(params.get("port", _DEFAULT_PORT)),
                database=params.get("dbname") or params.get("database"),
            )
        search_path = _resolve_search_path(params)
        self._connect_args: dict[str, object] = {}
        if search_path:
            self._connect_args["server_settings"] = {"search_path": search_path}
        ssl_param = params.get("ssl")
        if ssl_param is not None:
            self._connect_args["ssl"] = ssl_param
        self._schemas_filter: list[str] | None = params.get("schemas")
        self._tables_filter: list[str] | None = params.get("tables")
        self._row_limit = int(params.get("row_limit", _DEFAULT_ROW_LIMIT))
        self._statement_timeout_ms = int(
            params.get("statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS)
        )
        self._fetch_column_stats = bool(params.get("fetch_column_stats", False))
        self._connection_id = connection.id
        self._engine: AsyncEngine | None = None

    @property
    def dsn(self) -> str:
        """The async SQLAlchemy DSN (password rendered, for diagnostics/tests)."""
        return self._url.render_as_string(hide_password=False)

    def _get_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(self._url, connect_args=self._connect_args)
        return self._engine

    async def aclose(self) -> None:
        """Dispose the underlying engine and its connection pool."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    def capabilities(self) -> list[Capability]:
        return [
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
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
            relations = filter_relations(relations, self._schemas_filter, self._tables_filter)
            columns = await self._fetch_columns(conn)
            primary_keys = await self._fetch_primary_keys(conn)
            foreign_keys = await self._fetch_foreign_keys(conn)
            row_estimates = await self._fetch_row_estimates(conn)
            column_stats: dict[tuple[str, str], dict[str, tuple[float | None, float | None]]] = {}
            if self._fetch_column_stats:
                column_stats = await self._fetch_column_stats_map(conn)

        schemas: list[RelationSchema] = []
        for (schema, name), kind in sorted(relations.items()):
            relation = f"{schema}.{name}"
            cols = columns.get((schema, name), [])
            if not cols:
                continue
            pk = primary_keys.get((schema, name), [])
            fks = foreign_keys.get((schema, name), [])
            estimate = row_estimates.get((schema, name))
            if self._fetch_column_stats:
                cols = _apply_column_stats(cols, column_stats.get((schema, name), {}), estimate)
            schemas.append(
                RelationSchema(
                    connection=self._connection_id,
                    relation=relation,
                    kind=kind,  # type: ignore[arg-type]
                    columns=cols,
                    primary_key=pk,
                    foreign_keys=fks,
                    row_count_estimate=estimate,
                    acquisition_tier=AcquisitionTier.LIVE,
                    source_fingerprint=compute_fingerprint(cols, pk, fks),
                )
            )
        return schemas

    async def _fetch_relations(self, conn: Any) -> dict[tuple[str, str], str]:
        result = await conn.execute(
            text(
                "SELECT table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN "
                "  ('pg_catalog', 'information_schema', 'pg_internal', 'pg_toast') "
                "AND table_schema NOT LIKE 'pg_temp_%'"
            )
        )
        relations: dict[tuple[str, str], str] = {}
        for schema, name, table_type in result:
            kind = _KIND_BY_TABLE_TYPE.get(table_type)
            if kind is not None:
                relations[(schema, name)] = kind
        # Redshift materialized views are not in information_schema.tables.
        try:
            mv_result = await conn.execute(text("SELECT schema_name, mv_name FROM SVV_MV_INFO"))
            for schema, name in mv_result:
                relations[(schema, name)] = "materialized_view"
        except Exception as exc:
            logger.warning("could not fetch Redshift materialized views from SVV_MV_INFO: %s", exc)
            await conn.rollback()
        return relations

    async def _fetch_columns(self, conn: Any) -> dict[tuple[str, str], list[ColumnInfo]]:
        out: dict[tuple[str, str], list[ColumnInfo]] = {}
        result = await conn.execute(
            text(
                "SELECT table_schema, table_name, column_name, data_type, "
                "is_nullable, ordinal_position "
                "FROM information_schema.columns "
                "WHERE table_schema NOT IN "
                "  ('pg_catalog', 'information_schema', 'pg_internal', 'pg_toast') "
                "AND table_schema NOT LIKE 'pg_temp_%' "
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
        # Supplement with materialized-view columns via pg_catalog; information_schema
        # does not expose matview columns in Redshift.
        try:
            matview_cols = await conn.execute(
                text(
                    "SELECT n.nspname, c.relname, a.attname, "
                    "format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attnum "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind = 'm' AND a.attnum > 0 AND NOT a.attisdropped "
                    "AND n.nspname NOT IN "
                    "  ('pg_catalog', 'information_schema', 'pg_internal', 'pg_toast') "
                    "AND n.nspname NOT LIKE 'pg_temp_%' "
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
        except Exception as exc:
            logger.warning(
                "could not fetch Redshift materialized-view columns from pg_catalog: %s", exc
            )
            await conn.rollback()
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
                "  AND tc.table_schema NOT IN "
                "  ('pg_catalog', 'information_schema', 'pg_internal', 'pg_toast') "
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
                "  AND tc.table_schema NOT IN "
                "  ('pg_catalog', 'information_schema', 'pg_internal', 'pg_toast') "
                "ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position"
            )
        )
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
                "  AND n.nspname NOT IN "
                "  ('pg_catalog', 'information_schema', 'pg_internal', 'pg_toast')"
            )
        )
        out: dict[tuple[str, str], int | None] = {}
        for schema, name, reltuples in result:
            out[(schema, name)] = (
                int(reltuples) if reltuples is not None and reltuples >= 0 else None
            )
        return out

    async def _fetch_column_stats_map(
        self, conn: Any
    ) -> dict[tuple[str, str], dict[str, tuple[float | None, float | None]]]:
        """Zero-scan column cardinality/null stats from pg_stats (opt-in via fetch_column_stats).

        Redshift is Postgres-catalog-compatible for pg_stats. Redshift has no autovacuum,
        so a relation that has never been manually ANALYZE'd simply has no pg_stats rows —
        callers treat absence as "no stats available", not as a sentinel value.
        """
        result = await conn.execute(
            text(
                "SELECT schemaname, tablename, attname, null_frac, n_distinct "
                "FROM pg_stats "
                "WHERE schemaname NOT IN "
                "  ('pg_catalog', 'information_schema', 'pg_internal', 'pg_toast')"
            )
        )
        out: dict[tuple[str, str], dict[str, tuple[float | None, float | None]]] = {}
        for schema, name, column, null_frac, n_distinct in result:
            out.setdefault((schema, name), {})[column] = (null_frac, n_distinct)
        return out

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        # Redshift does not support SET LOCAL default_transaction_read_only; the
        # parse-level assert_read_only() guard below is the enforcement mechanism.
        assert_read_only(sql)
        engine = self._get_engine()
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}"))
            result = await conn.stream(text(sql))
            keys = list(result.keys())
            fetched = await result.fetchmany(self._row_limit + 1)

        truncated = len(fetched) > self._row_limit
        rows = [list(row) for row in fetched[: self._row_limit]]
        columns = self._result_columns(keys, rows)
        return ResultSet(columns=columns, rows=rows, truncated=truncated, bytes_scanned=None)

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        """Observe a relation's columns via a zero-scan probe (SPEC-E2 §5).

        Uses the asyncpg extended-query ``Parse`` message so no rows are scanned.
        A non-existent relation surfaces the driver's ``UndefinedTableError``.
        """
        if not _SAFE_RELATION.match(relation):
            raise ValueError(f"unsafe relation identifier: {relation!r}")
        engine = self._get_engine()
        async with engine.connect() as conn, conn.begin():
            raw = await conn.get_raw_connection()
            asyncpg_conn = raw.driver_connection
            stmt = await asyncpg_conn.prepare(f"SELECT * FROM {relation} WHERE false")  # type: ignore[union-attr]
            attributes = stmt.get_attributes()
        return [
            ColumnInfo(
                name=attr.name,
                type=_normalize_type(attr.type.name, relation, attr.name),
                nullable=True,
                position=i + 1,
            )
            for i, attr in enumerate(attributes)
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
