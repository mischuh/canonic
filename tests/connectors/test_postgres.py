"""Tests for the PostgreSQL connector (GH-4).

Unit tests cover read-only enforcement, type mapping, DSN building and the
capability surface with no database. Integration tests (``@pytest.mark.integration``)
exercise a real PostgreSQL via testcontainers; they skip when Docker is absent.
"""

from __future__ import annotations

import logging

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from canon.config import Connection
from canon.connectors.base import AcquisitionTier, Capability
from canon.connectors.postgres import PostgresConnector, _normalize_type, _resolve_search_path
from canon.exc import ReadOnlyViolation

# ---------------------------------------------------------------------------
# Unit: type mapping
# ---------------------------------------------------------------------------


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("integer", "int"),
            ("bigint", "int"),
            ("smallint", "int"),
            ("numeric(12,2)", "decimal"),
            ("numeric", "decimal"),
            ("double precision", "float"),
            ("boolean", "bool"),
            ("character varying(50)", "string"),
            ("text", "string"),
            ("uuid", "string"),
            ("date", "date"),
            ("timestamp with time zone", "timestamp"),
            ("timestamp without time zone", "timestamp"),
            ("jsonb", "json"),
            ("json", "json"),
        ],
    )
    def test_known_types(self, raw: str, expected: str) -> None:
        assert _normalize_type(raw, "analytics.t", "c") == expected

    def test_unmapped_type_falls_back_to_json_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert _normalize_type("geometry", "analytics.t", "geom") == "json"
        assert "geometry" in caplog.text

    def test_array_falls_back_to_json_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert _normalize_type("integer[]", "analytics.t", "tags") == "json"
        assert "tags" in caplog.text


# ---------------------------------------------------------------------------
# Unit: DSN + capabilities
# ---------------------------------------------------------------------------


class TestConnectorSurface:
    def test_dsn_is_async_with_rendered_password(
        self, offline_connector: PostgresConnector
    ) -> None:
        dsn = offline_connector.dsn
        assert dsn.startswith("postgresql+asyncpg://")
        assert "u:secret@localhost:5432/db" in dsn

    def test_capabilities(self, offline_connector: PostgresConnector) -> None:
        assert set(offline_connector.capabilities()) == {
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        }


class TestSearchPathPrecedence:
    def test_schemas_list_takes_precedence_over_legacy_schema(self) -> None:
        params = {"schemas": ["finance", "public"], "schema": "legacy"}
        assert _resolve_search_path(params) == "finance,public"

    def test_legacy_schema_used_when_schemas_absent(self) -> None:
        assert _resolve_search_path({"schema": "legacy"}) == "legacy"

    def test_none_when_neither_present(self) -> None:
        assert _resolve_search_path({}) is None


# ---------------------------------------------------------------------------
# Integration: real PostgreSQL (testcontainers)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPostgresIntegration:
    async def test_connection_ok(self, pg_connector: PostgresConnector) -> None:
        health = await pg_connector.test_connection()
        assert health.status == "ok"

    async def test_connection_bad_credentials(
        self,
        postgres_container: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CANON_TEST_BAD_PW", "definitely-wrong")
        connection = Connection(
            id="warehouse_pg",
            type="postgres",
            params=postgres_container["params"],
            credentials_ref="env:CANON_TEST_BAD_PW",
        )
        connector = PostgresConnector(connection)
        try:
            health = await connector.test_connection()
        finally:
            await connector.aclose()
        assert health.status == "error"
        assert health.message

    async def test_introspection_emits_normalized_evidence(
        self, pg_connector: PostgresConnector
    ) -> None:
        schemas = {s.relation: s for s in await pg_connector.introspect_schema()}
        assert "analytics.fct_orders" in schemas

        orders = schemas["analytics.fct_orders"]
        assert orders.connection == "warehouse_pg"
        assert orders.kind == "table"
        assert orders.acquisition_tier == AcquisitionTier.LIVE
        assert orders.primary_key == ["order_id"]
        assert orders.source_fingerprint is not None
        assert orders.source_fingerprint.startswith("sha256:")

        col_types = {c.name: c.type for c in orders.columns}
        assert col_types["order_id"] == "int"
        assert col_types["customer_id"] == "int"
        assert col_types["amount"] == "decimal"
        assert col_types["metadata"] == "json"
        assert col_types["order_date"] == "date"

        assert any(
            fk.references.relation == "analytics.dim_customers" and fk.columns == ["customer_id"]
            for fk in orders.foreign_keys
        )

        # fetch_column_stats defaults to False — today's zero-scan behavior is unchanged.
        order_id_col = next(c for c in orders.columns if c.name == "order_id")
        assert order_id_col.stats_source is None
        assert order_id_col.distinct_count_estimate is None
        assert order_id_col.null_fraction is None
        assert order_id_col.uniqueness_ratio is None

    async def test_introspection_with_fetch_column_stats_populates_stats_fields(
        self, postgres_container: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CANON_TEST_PG_PASSWORD", postgres_container["password"])
        params = postgres_container["params"]
        dsn = (
            f"postgresql://{params['user']}:{postgres_container['password']}"
            f"@{params['host']}:{params['port']}/{params['dbname']}"
        )
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "INSERT INTO analytics.dim_customers (customer_id, name) VALUES "
                "(90001, 'stats-a'), (90002, 'stats-b') ON CONFLICT DO NOTHING"
            )
            await conn.execute(
                "INSERT INTO analytics.fct_orders (order_id, customer_id, amount) VALUES "
                "(90001, 90001, 10.0), (90002, 90001, 20.0), (90003, 90002, NULL) "
                "ON CONFLICT DO NOTHING"
            )
            await conn.execute("ANALYZE analytics.fct_orders")
        finally:
            await conn.close()

        connection = Connection(
            id="warehouse_pg",
            type="postgres",
            params={**params, "fetch_column_stats": True},
            credentials_ref="env:CANON_TEST_PG_PASSWORD",
        )
        connector = PostgresConnector(connection)
        try:
            schemas = {s.relation: s for s in await connector.introspect_schema()}
        finally:
            await connector.aclose()

        orders = schemas["analytics.fct_orders"]
        order_id_col = next(c for c in orders.columns if c.name == "order_id")
        amount_col = next(c for c in orders.columns if c.name == "amount")

        assert order_id_col.stats_source == "pg_stats"
        assert order_id_col.null_fraction == 0.0
        assert order_id_col.distinct_count_estimate is not None
        assert amount_col.stats_source == "pg_stats"
        assert amount_col.null_fraction is not None
        assert amount_col.null_fraction > 0.0

    async def test_select_returns_typed_resultset(self, pg_connector: PostgresConnector) -> None:
        result = await pg_connector.run_read_only_sql("SELECT 1 AS a, 'x' AS b")
        assert [c.name for c in result.columns] == ["a", "b"]
        assert {c.type for c in result.columns} == {"int", "string"}
        assert result.rows == [[1, "x"]]
        assert result.truncated is False

    async def test_row_limit_is_enforced(self, pg_connector: PostgresConnector) -> None:
        # pg_connector fixture sets row_limit=5
        result = await pg_connector.run_read_only_sql("SELECT g FROM generate_series(1, 100) AS g")
        assert len(result.rows) == 5
        assert result.truncated is True

    async def test_insert_rejected_before_execution(self, pg_connector: PostgresConnector) -> None:
        with pytest.raises(ReadOnlyViolation):
            await pg_connector.run_read_only_sql(
                "INSERT INTO analytics.dim_customers (customer_id, name) VALUES (999, 'z')"
            )
        # the row was never written
        result = await pg_connector.run_read_only_sql(
            "SELECT count(*) AS n FROM analytics.dim_customers WHERE customer_id = 999"
        )
        assert result.rows == [[0]]

    async def test_statement_timeout_is_enforced(self, pg_connector: PostgresConnector) -> None:
        # pg_connector fixture sets statement_timeout_ms=5000. The cancellation
        # may surface raw (asyncpg) or wrapped (SQLAlchemy) depending on the path.
        with pytest.raises((DBAPIError, asyncpg.PostgresError)):
            await pg_connector.run_read_only_sql("SELECT pg_sleep(30)")

    async def test_introspection_excludes_unselected_schema(
        self, postgres_container: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CANON_TEST_PG_PASSWORD", postgres_container["password"])
        connection = Connection(
            id="warehouse_pg",
            type="postgres",
            params={**postgres_container["params"], "schemas": ["nonexistent"]},
            credentials_ref="env:CANON_TEST_PG_PASSWORD",
        )
        connector = PostgresConnector(connection)
        try:
            relations = await connector.introspect_schema()
        finally:
            await connector.aclose()
        assert relations == []

    async def test_introspection_filters_by_table_glob(
        self, postgres_container: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CANON_TEST_PG_PASSWORD", postgres_container["password"])
        connection = Connection(
            id="warehouse_pg",
            type="postgres",
            params={**postgres_container["params"], "tables": ["fct_*"]},
            credentials_ref="env:CANON_TEST_PG_PASSWORD",
        )
        connector = PostgresConnector(connection)
        try:
            relations = {r.relation for r in await connector.introspect_schema()}
        finally:
            await connector.aclose()
        assert relations == {"analytics.fct_orders"}
