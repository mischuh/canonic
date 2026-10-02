"""ClickHouse connector tests against a real server (testcontainers, skipped without Docker)."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import sqlglot
from clickhouse_connect.driver.exceptions import ClickHouseError
from sqlglot import exp

import canonic.connectors.clickhouse as clickhouse_module
from canonic.compiler.dialect import adapter_for
from canonic.config import Connection
from canonic.connectors.base import ReadOnlyEnforcement
from canonic.connectors.clickhouse import ClickHouseConnector
from canonic.exc import ReadOnlyViolation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.conftest import ClickHouseServer

pytestmark = pytest.mark.integration

_SEED = [
    """
    CREATE TABLE dim_customers (
        customer_id UInt32,
        name        String,
        active      Bool,
        created_at  Nullable(DateTime64(3, 'UTC'))
    ) ENGINE = MergeTree ORDER BY customer_id
    """,
    """
    CREATE TABLE fct_orders (
        order_id    UInt64,
        customer_id UInt32,
        amount      Nullable(Decimal(12, 2)),
        tags        Array(String),
        order_date  Date
    ) ENGINE = MergeTree ORDER BY order_id
    """,
    "CREATE VIEW v_order_totals AS "
    "SELECT customer_id, sum(amount) AS total FROM fct_orders GROUP BY customer_id",
    "INSERT INTO dim_customers VALUES (1, 'Ada', true, '2025-01-01 10:00:00.123'), (2, 'Bo', false, NULL)",
    "INSERT INTO fct_orders VALUES "
    + ", ".join(f"({i}, {1 + i % 2}, {i}.50, ['t{i}'], '2025-01-0{i}')" for i in range(1, 8)),
]

_DATABASE = "analytics"
_PASSWORD_ENV = "CANONIC_TEST_CLICKHOUSE_PASSWORD"


@pytest.fixture(scope="module")
def analytics(clickhouse_server: ClickHouseServer) -> str:
    clickhouse_server.new_database(_DATABASE)
    clickhouse_server.execute(_SEED, database=_DATABASE)
    return _DATABASE


def _connector(
    server: ClickHouseServer,
    monkeypatch: pytest.MonkeyPatch,
    *,
    password: str | None = None,
    **params: Any,
) -> ClickHouseConnector:
    monkeypatch.setenv(_PASSWORD_ENV, server.password if password is None else password)
    return ClickHouseConnector(
        Connection(
            id="events",
            type="clickhouse",
            params={"host": server.host, "port": server.port, "user": server.user, **params},
            credentials_ref=f"env:{_PASSWORD_ENV}",
        )
    )


@pytest.fixture
async def connector(
    clickhouse_server: ClickHouseServer, analytics: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ClickHouseConnector]:
    yield _connector(clickhouse_server, monkeypatch, database=analytics)


class TestConnection:
    async def test_connection_is_ok(self, connector: ClickHouseConnector) -> None:
        health = await connector.test_connection()
        assert health.status == "ok", health.message
        assert health.warnings == ()

    async def test_wrong_password_is_reported(
        self, clickhouse_server: ClickHouseServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = _connector(clickhouse_server, monkeypatch, password="not-the-password")
        health = await connector.test_connection()
        assert health.status == "error"

    def test_enforcement_is_native(self, connector: ClickHouseConnector) -> None:
        assert connector.read_only_enforcement() is ReadOnlyEnforcement.NATIVE


class TestIntrospection:
    async def test_relations_and_kinds(self, connector: ClickHouseConnector) -> None:
        schemas = {
            s.relation: s
            for s in await connector.introspect_schema()
            if s.relation.startswith(f"{_DATABASE}.")
        }

        assert set(schemas) == {
            f"{_DATABASE}.dim_customers",
            f"{_DATABASE}.fct_orders",
            f"{_DATABASE}.v_order_totals",
        }
        assert schemas[f"{_DATABASE}.fct_orders"].kind == "table"
        assert schemas[f"{_DATABASE}.v_order_totals"].kind == "view"
        assert schemas[f"{_DATABASE}.fct_orders"].row_count_estimate == 7

    async def test_column_types_and_nullability(self, connector: ClickHouseConnector) -> None:
        schemas = {s.relation: s for s in await connector.introspect_schema()}

        orders = {c.name: c for c in schemas[f"{_DATABASE}.fct_orders"].columns}
        assert (orders["order_id"].type, orders["order_id"].nullable) == ("int", False)
        assert (orders["amount"].type, orders["amount"].nullable) == ("decimal", True)
        assert orders["tags"].type == "json"
        assert orders["order_date"].type == "date"

        customers = {c.name: c for c in schemas[f"{_DATABASE}.dim_customers"].columns}
        assert customers["active"].type == "bool"
        assert customers["created_at"].type == "timestamp"

    async def test_no_key_is_asserted_and_system_databases_are_hidden(
        self, connector: ClickHouseConnector
    ) -> None:
        schemas = await connector.introspect_schema()
        assert all(s.primary_key == [] and s.foreign_keys == [] for s in schemas)
        assert not any(
            s.relation.split(".")[0] in {"system", "INFORMATION_SCHEMA"} for s in schemas
        )

    async def test_tables_filter(
        self,
        clickhouse_server: ClickHouseServer,
        analytics: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        narrowed = _connector(
            clickhouse_server,
            monkeypatch,
            database=analytics,
            schemas=[analytics],
            tables=["fct_*"],
        )
        assert [s.relation for s in await narrowed.introspect_schema()] == [
            f"{_DATABASE}.fct_orders"
        ]

    async def test_describe_relation(self, connector: ClickHouseConnector) -> None:
        qualified = await connector.describe_relation(f"{_DATABASE}.fct_orders")
        bare = await connector.describe_relation("fct_orders")

        assert [(c.name, c.type) for c in qualified] == [
            ("order_id", "int"),
            ("customer_id", "int"),
            ("amount", "decimal"),
            ("tags", "json"),
            ("order_date", "date"),
        ]
        assert qualified == bare
        assert [c.position for c in qualified] == [1, 2, 3, 4, 5]

    async def test_describe_missing_relation(self, connector: ClickHouseConnector) -> None:
        with pytest.raises(ValueError, match="not found"):
            await connector.describe_relation(f"{_DATABASE}.nope")


class TestRunReadOnlySql:
    async def test_runs_a_query_and_types_the_columns(self, connector: ClickHouseConnector) -> None:
        result = await connector.run_read_only_sql(
            "SELECT order_id, amount, order_date FROM fct_orders ORDER BY order_id LIMIT 2"
        )

        assert [(c.name, c.type) for c in result.columns] == [
            ("order_id", "int"),
            ("amount", "decimal"),
            ("order_date", "date"),
        ]
        assert result.rows == [
            [1, Decimal("1.50"), datetime.date(2025, 1, 1)],
            [2, Decimal("2.50"), datetime.date(2025, 1, 2)],
        ]
        assert result.truncated is False

    async def test_row_limit_truncates(
        self,
        clickhouse_server: ClickHouseServer,
        analytics: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        limited = _connector(clickhouse_server, monkeypatch, database=analytics, row_limit=3)
        result = await limited.run_read_only_sql("SELECT number FROM numbers(1000000)")
        assert len(result.rows) == 3
        assert result.truncated is True

    async def test_parser_refuses_writes_before_connecting(
        self, connector: ClickHouseConnector
    ) -> None:
        with pytest.raises(ReadOnlyViolation):
            await connector.run_read_only_sql("INSERT INTO fct_orders (order_id) VALUES (99)")

    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO fct_orders (order_id) VALUES (99)",
            "CREATE TABLE sneaky (a Int8) ENGINE = Memory",
            "DROP TABLE dim_customers",
            "ALTER TABLE fct_orders DELETE WHERE 1",
            "TRUNCATE TABLE fct_orders",
        ],
    )
    async def test_server_refuses_writes_even_if_the_parser_is_bypassed(
        self,
        connector: ClickHouseConnector,
        monkeypatch: pytest.MonkeyPatch,
        statement: str,
    ) -> None:
        monkeypatch.setattr(clickhouse_module, "assert_read_only", lambda *_a, **_k: None)
        with pytest.raises(ClickHouseError, match="readonly"):
            await connector.run_read_only_sql(statement)

        remaining = await connector.run_read_only_sql("SELECT count() FROM fct_orders")
        assert remaining.rows == [[7]]

    async def test_a_query_cannot_lift_the_read_only_setting(
        self, connector: ClickHouseConnector
    ) -> None:
        with pytest.raises(ClickHouseError, match="readonly"):
            await connector.run_read_only_sql("SELECT 1 SETTINGS readonly = 0")

    async def test_statement_timeout_is_enforced_by_the_server(
        self,
        clickhouse_server: ClickHouseServer,
        analytics: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        quick = _connector(
            clickhouse_server, monkeypatch, database=analytics, statement_timeout_ms=1000
        )
        sql = "SELECT sleepEachRow(0.4) FROM numbers(10) SETTINGS max_block_size = 1"
        with pytest.raises(ClickHouseError, match="(?i)timeout"):
            await quick.run_read_only_sql(sql)


class TestDialectAdapterOnClickHouse:
    """The SQL the adapter emits must run on a real server and give the same numbers."""

    async def _run(self, connector: ClickHouseConnector, neutral: exp.Expression) -> list[Any]:
        return (await connector.run_read_only_sql(adapter_for("clickhouse").emit(neutral))).rows

    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            ("day", datetime.datetime(2025, 3, 12)),
            ("week", datetime.date(2025, 3, 10)),
            ("month", datetime.date(2025, 3, 1)),
            ("quarter", datetime.date(2025, 1, 1)),
            ("year", datetime.date(2025, 1, 1)),
        ],
    )
    async def test_date_trunc_buckets_to_the_start_of_the_period(
        self, connector: ClickHouseConnector, unit: str, expected: datetime.date | datetime.datetime
    ) -> None:
        """2025-03-12 is a Wednesday, so a week starts on Monday the 10th."""
        trunc = exp.func("DATE_TRUNC", exp.Literal.string(unit), exp.cast("d", "date"))
        neutral = exp.select(trunc).from_(
            exp.alias_(exp.select(exp.Literal.string("2025-03-12").as_("d")).subquery(), "o")
        )
        assert await self._run(connector, neutral) == [[expected]]

    async def test_week_start_on_a_sunday_stays_in_the_previous_week(
        self, connector: ClickHouseConnector
    ) -> None:
        trunc = exp.func("DATE_TRUNC", exp.Literal.string("week"), exp.cast("'2025-03-16'", "date"))
        assert await self._run(connector, exp.select(trunc)) == [[datetime.date(2025, 3, 10)]]

    async def test_watermark_keeps_the_offset_on_every_server_version(
        self, connector: ClickHouseConnector
    ) -> None:
        """23:59:59 at UTC-4 is 03:59:59 UTC on the 4th, so orders 1 to 4 are at or before it.

        A plain ``CAST`` to ``DateTime`` yields NULL on ClickHouse 25.8, which would count 0."""
        neutral = sqlglot.parse_one(
            "SELECT count(*) FROM fct_orders WHERE order_date <= "
            "CAST('2025-01-03T23:59:59-04:00' AS TIMESTAMPTZ)",
            dialect="postgres",
        )
        assert await self._run(connector, neutral) == [[4]]

    async def test_bare_decimal_cast_keeps_its_fraction(
        self, connector: ClickHouseConnector
    ) -> None:
        neutral = sqlglot.parse_one("SELECT CAST('1.2345' AS NUMERIC)", dialect="postgres")
        assert await self._run(connector, neutral) == [[Decimal("1.2345")]]

    async def test_decimal_ratio_keeps_its_fraction(self, connector: ClickHouseConnector) -> None:
        """Orders 1, 2 and 4 sum to 8.50, so the mean is 2.8333... A plain ``Decimal(12, 2) /
        UInt64`` would truncate it to 2.83."""
        neutral = sqlglot.parse_one(
            "SELECT SUM(amount) / NULLIF(COUNT(order_id), 0) FROM fct_orders "
            "WHERE order_id IN (1, 2, 4)",
            dialect="postgres",
        )
        [[value]] = await self._run(connector, neutral)
        assert value == pytest.approx(8.5 / 3, rel=1e-12)

    async def test_left_join_yields_null_not_a_default(
        self, connector: ClickHouseConnector
    ) -> None:
        """Without ``join_use_nulls`` the unmatched order would show ``0``, not NULL."""
        neutral = sqlglot.parse_one(
            "SELECT o.order_id, c.customer_id FROM fct_orders o "
            "LEFT JOIN (SELECT customer_id FROM dim_customers WHERE customer_id = 1) c "
            "ON o.customer_id = c.customer_id ORDER BY o.order_id LIMIT 2",
            dialect="postgres",
        )
        assert await self._run(connector, neutral) == [[1, None], [2, 1]]

    @pytest.mark.parametrize(("order", "expected"), [("", 3.0), (" DESC", 6.0)])
    async def test_percentile_matches_percentile_cont(
        self, connector: ClickHouseConnector, order: str, expected: float
    ) -> None:
        """Amounts are 1.5 to 7.5. The 25th percentile sits at position 1.5 of the ordered list
        and interpolates between its neighbours, from the top of the list when descending. The
        column is ``Decimal``, which ``quantileExactInclusive`` only accepts after a cast."""
        neutral = sqlglot.parse_one(
            f"SELECT PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY amount{order}) FROM fct_orders",
            dialect="postgres",
        )
        [[value]] = await self._run(connector, neutral)
        assert value == pytest.approx(expected)
