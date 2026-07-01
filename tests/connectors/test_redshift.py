"""Tests for the Redshift connector.

Unit tests cover type mapping, DSN building, and the capability surface with no
database.  Integration tests (``@pytest.mark.integration``) run against the
PostgreSQL testcontainer, which is wire-protocol-compatible with Redshift for
the operations exercised here; Redshift-specific views (SVV_MV_INFO) are
expected to log a warning and be skipped gracefully.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from canon.config import Connection
from canon.connectors.base import AcquisitionTier, Capability
from canon.connectors.redshift import RedshiftConnector, _normalize_type, _resolve_search_path
from canon.exc import ReadOnlyViolation


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("integer", "int"),
            ("bigint", "int"),
            ("smallint", "int"),
            ("int2", "int"),
            ("int4", "int"),
            ("int8", "int"),
            ("numeric(18,2)", "decimal"),
            ("numeric", "decimal"),
            ("decimal", "decimal"),
            ("double precision", "float"),
            ("float4", "float"),
            ("float8", "float"),
            ("real", "float"),
            ("boolean", "bool"),
            ("bool", "bool"),
            ("character varying(256)", "string"),
            ("varchar(256)", "string"),
            ("nvarchar(256)", "string"),
            ("character(1)", "string"),
            ("char(10)", "string"),
            ("text", "string"),
            ("date", "date"),
            ("timestamp with time zone", "timestamp"),
            ("timestamp without time zone", "timestamp"),
            ("timestamptz", "timestamp"),
            ("timestamp", "timestamp"),
            ("time without time zone", "string"),
            # Redshift-specific types
            ("super", "json"),
            ("hllsketch", "json"),
            ("geometry", "json"),
            ("geography", "json"),
            ("varbyte", "json"),
        ],
    )
    def test_known_types(self, raw: str, expected: str) -> None:
        assert _normalize_type(raw, "analytics.t", "c") == expected

    def test_unmapped_type_falls_back_to_json_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert _normalize_type("timezoneoid_unknown", "analytics.t", "tz") == "json"
        assert "timezoneoid_unknown" in caplog.text

    def test_array_falls_back_to_json_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            assert _normalize_type("integer[]", "analytics.t", "tags") == "json"
        assert "tags" in caplog.text


class TestConnectorSurface:
    def test_dsn_uses_asyncpg_driver(self, offline_redshift_connector: RedshiftConnector) -> None:
        dsn = offline_redshift_connector.dsn
        assert dsn.startswith("redshift+asyncpg://")

    def test_dsn_contains_host_and_credentials(
        self, offline_redshift_connector: RedshiftConnector
    ) -> None:
        dsn = offline_redshift_connector.dsn
        assert "u:secret@redshift.example.com:5439/db" in dsn

    def test_capabilities(self, offline_redshift_connector: RedshiftConnector) -> None:
        assert set(offline_redshift_connector.capabilities()) == {
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        }

    def test_default_port_is_5439(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CANON_TEST_RS_PASSWORD", "pw")
        conn = Connection(
            id="rs",
            type="redshift",
            params={"host": "redshift.example.com", "user": "u", "dbname": "db"},
            credentials_ref="env:CANON_TEST_RS_PASSWORD",
        )
        connector = RedshiftConnector(conn)
        assert ":5439/" in connector.dsn

    def test_dsn_from_url_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "CANON_TEST_RS_DSN",
            "postgresql://admin:pw@my-cluster.us-east-1.redshift.amazonaws.com:5439/analytics",
        )
        conn = Connection(
            id="rs",
            type="redshift",
            params={},
            credentials_ref="env:CANON_TEST_RS_DSN",
        )
        connector = RedshiftConnector(conn)
        assert connector.dsn.startswith("redshift+asyncpg://")
        assert "my-cluster.us-east-1.redshift.amazonaws.com" in connector.dsn


class TestSearchPathPrecedence:
    def test_schemas_list_takes_precedence_over_legacy_schema(self) -> None:
        params = {"schemas": ["finance", "public"], "schema": "legacy"}
        assert _resolve_search_path(params) == "finance,public"

    def test_legacy_schema_used_when_schemas_absent(self) -> None:
        assert _resolve_search_path({"schema": "legacy"}) == "legacy"

    def test_none_when_neither_present(self) -> None:
        assert _resolve_search_path({}) is None


@pytest.mark.integration
class TestRedshiftIntegration:
    """Integration suite using a PostgreSQL container as a Redshift wire-compatible proxy.

    Redshift-specific views (SVV_MV_INFO, pg_internal) are absent in PostgreSQL;
    the connector handles those gracefully via logged warnings.
    """

    async def test_connection_ok(self, redshift_connector: RedshiftConnector) -> None:
        health = await redshift_connector.test_connection()
        assert health.status == "ok"

    async def test_connection_bad_credentials(
        self,
        postgres_container: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CANON_TEST_BAD_RS_PW", "definitely-wrong")
        connection = Connection(
            id="warehouse_rs",
            type="redshift",
            params=postgres_container["params"],
            credentials_ref="env:CANON_TEST_BAD_RS_PW",
        )
        connector = RedshiftConnector(connection)
        try:
            health = await connector.test_connection()
        finally:
            await connector.aclose()
        assert health.status == "error"
        assert health.message

    async def test_introspection_emits_normalized_evidence(
        self, redshift_connector: RedshiftConnector
    ) -> None:
        schemas = {s.relation: s for s in await redshift_connector.introspect_schema()}
        assert "analytics.fct_orders" in schemas

        orders = schemas["analytics.fct_orders"]
        assert orders.connection == "warehouse_rs"
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

    async def test_select_returns_typed_resultset(
        self, redshift_connector: RedshiftConnector
    ) -> None:
        result = await redshift_connector.run_read_only_sql("SELECT 1 AS a, 'x' AS b")
        assert [c.name for c in result.columns] == ["a", "b"]
        assert result.rows == [[1, "x"]]
        assert result.truncated is False

    async def test_row_limit_is_enforced(self, redshift_connector: RedshiftConnector) -> None:
        result = await redshift_connector.run_read_only_sql(
            "SELECT g FROM generate_series(1, 100) AS g"
        )
        assert len(result.rows) == 5
        assert result.truncated is True

    async def test_insert_rejected_before_execution(
        self, redshift_connector: RedshiftConnector
    ) -> None:
        with pytest.raises(ReadOnlyViolation):
            await redshift_connector.run_read_only_sql(
                "INSERT INTO analytics.dim_customers (customer_id, name) VALUES (999, 'z')"
            )
        result = await redshift_connector.run_read_only_sql(
            "SELECT count(*) AS n FROM analytics.dim_customers WHERE customer_id = 999"
        )
        assert result.rows == [[0]]

    async def test_statement_timeout_is_enforced(
        self, redshift_connector: RedshiftConnector
    ) -> None:
        with pytest.raises((DBAPIError, Exception)):
            await redshift_connector.run_read_only_sql("SELECT pg_sleep(30)")

    async def test_introspection_excludes_unselected_schema(
        self, postgres_container: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CANON_TEST_RS_PASSWORD", postgres_container["password"])
        connection = Connection(
            id="warehouse_rs",
            type="redshift",
            params={**postgres_container["params"], "schemas": ["nonexistent"]},
            credentials_ref="env:CANON_TEST_RS_PASSWORD",
        )
        connector = RedshiftConnector(connection)
        try:
            relations = await connector.introspect_schema()
        finally:
            await connector.aclose()
        assert relations == []

    async def test_introspection_filters_by_table_glob(
        self, postgres_container: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CANON_TEST_RS_PASSWORD", postgres_container["password"])
        connection = Connection(
            id="warehouse_rs",
            type="redshift",
            params={**postgres_container["params"], "tables": ["fct_*"]},
            credentials_ref="env:CANON_TEST_RS_PASSWORD",
        )
        connector = RedshiftConnector(connection)
        try:
            relations = {r.relation for r in await connector.introspect_schema()}
        finally:
            await connector.aclose()
        assert relations == {"analytics.fct_orders"}
