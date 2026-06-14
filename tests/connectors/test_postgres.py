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
from canon.connectors.postgres import PostgresConnector, _normalize_type
from canon.exc import ReadOnlyViolation

# ---------------------------------------------------------------------------
# Unit: read-only enforcement (parse level, no DB)
# ---------------------------------------------------------------------------


class TestReadOnlyEnforcement:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT a, b FROM analytics.fct_orders WHERE a > 1",
            "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
            "SELECT 1 UNION SELECT 2",
        ],
    )
    def test_select_allowed(self, offline_connector: PostgresConnector, sql: str) -> None:
        offline_connector._assert_read_only(sql)  # must not raise

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE t (a int)",
            "TRUNCATE t",
            "SELECT 1; SELECT 2",
            "SELECT 1; DROP TABLE t",
        ],
    )
    def test_non_select_rejected(self, offline_connector: PostgresConnector, sql: str) -> None:
        with pytest.raises(ReadOnlyViolation):
            offline_connector._assert_read_only(sql)


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
            Capability.introspect_schema,
            Capability.run_read_only_sql,
            Capability.test_connection,
            Capability.capabilities,
        }


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
        assert orders.acquisition_tier == AcquisitionTier.live
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
