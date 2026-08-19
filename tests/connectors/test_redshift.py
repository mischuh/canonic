"""Tests for the Redshift connector.

Unit tests cover type mapping, DSN building, and the capability surface with no
database.  Integration tests (``@pytest.mark.integration``) run against the
PostgreSQL testcontainer, which is wire-protocol-compatible with Redshift for
the operations exercised here; Redshift-specific views (SVV_MV_INFO) are
expected to log a warning and be skipped gracefully.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

import canonic.credentials
from canonic.config import Connection
from canonic.connectors.base import AcquisitionTier, Capability
from canonic.connectors.redshift import RedshiftConnector, _normalize_type, _resolve_search_path
from canonic.credentials import CredentialProviderRegistry, ResolvedCredential
from canonic.exc import CredentialError, ReadOnlyViolation, UnknownCredentialProvider


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
        monkeypatch.setenv("CANONIC_TEST_RS_PASSWORD", "pw")
        conn = Connection(
            id="rs",
            type="redshift",
            params={"host": "redshift.example.com", "user": "u", "dbname": "db"},
            credentials_ref="env:CANONIC_TEST_RS_PASSWORD",
        )
        connector = RedshiftConnector(conn)
        assert ":5439/" in connector.dsn

    def test_dsn_from_url_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "CANONIC_TEST_RS_DSN",
            "postgresql://admin:pw@my-cluster.us-east-1.redshift.amazonaws.com:5439/analytics",
        )
        conn = Connection(
            id="rs",
            type="redshift",
            params={},
            credentials_ref="env:CANONIC_TEST_RS_DSN",
        )
        connector = RedshiftConnector(conn)
        assert connector.dsn.startswith("redshift+asyncpg://")
        assert "my-cluster.us-east-1.redshift.amazonaws.com" in connector.dsn


class _FakeRedshiftIamProvider:
    """Stands in for the AWS provider, issuing a numbered token with a chosen lifetime.

    ``ttl`` shorter than ``REFRESH_SKEW`` models a credential that is already past its
    usable life by the time the next connect asks for it — the "more than the token's
    TTL between calls" case, without making the test wait.
    """

    def __init__(self, *, ttl: timedelta = timedelta(minutes=15)) -> None:
        self.calls = 0
        self._ttl = ttl

    def get(self) -> ResolvedCredential:
        self.calls += 1
        return ResolvedCredential(
            value=f"token-{self.calls}",
            expires_at=datetime.now(UTC) + self._ttl,
            username=f"IAM:role-{self.calls}",
        )


class _FailingProvider:
    """A provider whose underlying AWS call always fails."""

    def get(self) -> ResolvedCredential:
        raise CredentialError("GetClusterCredentials denied")


def _register(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    """Make ``provider:fake-iam`` resolve to ``provider`` instead of touching AWS."""
    registry = CredentialProviderRegistry()
    registry.register("fake-iam", lambda params: provider)
    monkeypatch.setattr(canonic.credentials, "default_provider_registry", registry)


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> _FakeRedshiftIamProvider:
    provider = _FakeRedshiftIamProvider()
    _register(monkeypatch, provider)
    return provider


def _provider_backed_connection() -> Connection:
    return Connection(
        id="rs",
        type="redshift",
        params={
            "host": "127.0.0.1",
            # Port 1 is refused immediately, so a connect attempt fails without a live
            # database and without waiting on a timeout.
            "port": 1,
            "dbname": "analytics",
            "db_user": "canonic_ro",
            "region": "eu-central-1",
        },
        credentials_ref="provider:fake-iam",
    )


class TestProviderBackedCredentials:
    """A ``provider:`` credentials_ref resolves per connect, not once at construction."""

    def test_construction_performs_no_fetch(self, fake_provider: _FakeRedshiftIamProvider) -> None:
        RedshiftConnector(_provider_backed_connection())
        assert fake_provider.calls == 0

    def test_dsn_carries_no_password(self, fake_provider: _FakeRedshiftIamProvider) -> None:
        # There is no fixed password to bake in — it is injected per connect.
        connector = RedshiftConnector(_provider_backed_connection())
        dsn = connector.dsn
        assert dsn.startswith("redshift+asyncpg://")
        assert "canonic_ro@127.0.0.1:1/analytics" in dsn
        assert "token-" not in dsn

    def test_injects_password_and_temporary_user(
        self, fake_provider: _FakeRedshiftIamProvider
    ) -> None:
        # Redshift IAM rotates the user alongside the password, so both are stamped onto
        # the driver's connect arguments.
        connector = RedshiftConnector(_provider_backed_connection())
        connector._credentials.fetch_if_stale()  # type: ignore[union-attr]
        cparams: dict[str, Any] = {"user": "canonic_ro"}

        connector._inject_credential(None, None, (), cparams)

        assert cparams == {"user": "IAM:role-1", "password": "token-1"}

    async def test_second_connect_inside_ttl_reuses_the_credential(
        self, fake_provider: _FakeRedshiftIamProvider
    ) -> None:
        # S2/AC1: two connects within the credential's TTL cost one fetch.
        connector = RedshiftConnector(_provider_backed_connection())

        await connector.test_connection()
        await connector.test_connection()

        assert fake_provider.calls == 1
        await connector.aclose()

    async def test_connect_after_ttl_fetches_a_fresh_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # S1/AC1: with more than the token's TTL between connects, the second connect
        # uses a freshly fetched credential rather than the first one.
        provider = _FakeRedshiftIamProvider(ttl=timedelta(seconds=5))
        _register(monkeypatch, provider)
        connector = RedshiftConnector(_provider_backed_connection())

        await connector.test_connection()
        await connector.test_connection()

        assert provider.calls == 2
        assert connector._credentials.cached_value().value == "token-2"  # type: ignore[union-attr]
        await connector.aclose()

    async def test_fetch_failure_is_reported_as_an_unhealthy_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # S4/AC1: the provider's error surfaces and the connect never happens.
        _register(monkeypatch, _FailingProvider())
        connector = RedshiftConnector(_provider_backed_connection())

        health = await connector.test_connection()

        assert health.status == "error"
        assert "GetClusterCredentials denied" in (health.message or "")

    async def test_fetch_failure_propagates_out_of_a_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unlike test_connection, run_read_only_sql reports failure by raising.
        _register(monkeypatch, _FailingProvider())
        connector = RedshiftConnector(_provider_backed_connection())

        with pytest.raises(CredentialError, match="denied"):
            await connector.run_read_only_sql("SELECT 1")

    def test_unknown_provider_name_fails_loud(self) -> None:
        # S3/AC1: no silent fallback; the error names what is registered.
        conn = Connection(
            id="rs",
            type="redshift",
            params={"host": "h", "dbname": "db"},
            credentials_ref="provider:does-not-exist",
        )
        with pytest.raises(UnknownCredentialProvider, match="does-not-exist"):
            RedshiftConnector(conn)


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
        monkeypatch.setenv("CANONIC_TEST_BAD_RS_PW", "definitely-wrong")
        connection = Connection(
            id="warehouse_rs",
            type="redshift",
            params=postgres_container["params"],
            credentials_ref="env:CANONIC_TEST_BAD_RS_PW",
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

        # fetch_column_stats defaults to False — today's zero-scan behavior is unchanged.
        order_id_col = next(c for c in orders.columns if c.name == "order_id")
        assert order_id_col.stats_source is None
        assert order_id_col.distinct_count_estimate is None

    async def test_introspection_with_fetch_column_stats_populates_stats_fields(
        self, postgres_container: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CANONIC_TEST_RS_PASSWORD", postgres_container["password"])
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
            id="warehouse_rs",
            type="redshift",
            params={**params, "fetch_column_stats": True},
            credentials_ref="env:CANONIC_TEST_RS_PASSWORD",
        )
        connector = RedshiftConnector(connection)
        try:
            schemas = {s.relation: s for s in await connector.introspect_schema()}
        finally:
            await connector.aclose()

        orders = schemas["analytics.fct_orders"]
        order_id_col = next(c for c in orders.columns if c.name == "order_id")
        amount_col = next(c for c in orders.columns if c.name == "amount")

        assert order_id_col.stats_source == "pg_stats"
        assert order_id_col.null_fraction == 0.0
        assert amount_col.stats_source == "pg_stats"
        assert amount_col.null_fraction is not None
        assert amount_col.null_fraction > 0.0

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
        monkeypatch.setenv("CANONIC_TEST_RS_PASSWORD", postgres_container["password"])
        connection = Connection(
            id="warehouse_rs",
            type="redshift",
            params={**postgres_container["params"], "schemas": ["nonexistent"]},
            credentials_ref="env:CANONIC_TEST_RS_PASSWORD",
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
        monkeypatch.setenv("CANONIC_TEST_RS_PASSWORD", postgres_container["password"])
        connection = Connection(
            id="warehouse_rs",
            type="redshift",
            params={**postgres_container["params"], "tables": ["fct_*"]},
            credentials_ref="env:CANONIC_TEST_RS_PASSWORD",
        )
        connector = RedshiftConnector(connection)
        try:
            relations = {r.relation for r in await connector.introspect_schema()}
        finally:
            await connector.aclose()
        assert relations == {"analytics.fct_orders"}
