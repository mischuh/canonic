"""MySQL connector tests against a real MySQL 8 server (testcontainers, skipped without Docker)."""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pymysql.err
import pytest
from sqlglot import exp

from canonic.compiler.dialect import adapter_for
from canonic.config import Connection
from canonic.connectors.mysql import MySQLConnector
from canonic.exc import ReadOnlyViolation

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.conftest import MySQLServer

pytestmark = pytest.mark.integration

_SEED = [
    """
    CREATE TABLE dim_customers (
        customer_id INT PRIMARY KEY,
        name        VARCHAR(100) NOT NULL,
        active      TINYINT(1),
        created_at  DATETIME
    )
    """,
    """
    CREATE TABLE fct_orders (
        order_id    BIGINT PRIMARY KEY,
        customer_id INT NOT NULL,
        amount      DECIMAL(12, 2),
        metadata    JSON,
        order_date  DATE,
        CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES dim_customers (customer_id)
    )
    """,
    "CREATE VIEW v_order_totals AS SELECT customer_id, SUM(amount) AS total FROM fct_orders GROUP BY customer_id",
    "INSERT INTO dim_customers VALUES (1, 'Ada', 1, '2025-01-01 10:00:00'), (2, 'Bo', 0, NULL)",
    "INSERT INTO fct_orders VALUES "
    + ", ".join(
        f"({i}, {1 + i % 2}, {i}.50, '{{\"n\": {i}}}', '2025-01-0{i}')" for i in range(1, 8)
    ),
]

_DATABASE = "analytics"


@pytest.fixture(scope="module")
def analytics(mysql_server: MySQLServer) -> str:
    mysql_server.new_database(_DATABASE)
    mysql_server.execute(_SEED, database=_DATABASE)
    return _DATABASE


def _connection(
    server: MySQLServer, database: str, monkeypatch: pytest.MonkeyPatch, **params: object
) -> Connection:
    monkeypatch.setenv("CANONIC_TEST_MYSQL_PASSWORD", server.password)
    return Connection(
        id="warehouse_mysql",
        type="mysql",
        params={
            "host": server.host,
            "port": server.port,
            "user": server.user,
            "database": database,
            **params,
        },
        credentials_ref="env:CANONIC_TEST_MYSQL_PASSWORD",
    )


@pytest.fixture
async def connector(
    mysql_server: MySQLServer, analytics: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[MySQLConnector]:
    conn = MySQLConnector(
        _connection(mysql_server, analytics, monkeypatch, row_limit=5, statement_timeout_ms=1000)
    )
    try:
        yield conn
    finally:
        await conn.aclose()


class TestConnection:
    async def test_connection_ok(self, connector: MySQLConnector) -> None:
        assert (await connector.test_connection()).status == "ok"

    async def test_bad_credentials_are_reported(
        self, mysql_server: MySQLServer, analytics: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = _connection(mysql_server, analytics, monkeypatch)
        monkeypatch.setenv("CANONIC_TEST_MYSQL_PASSWORD", "definitely-wrong")
        bad = MySQLConnector(connection)
        try:
            health = await bad.test_connection()
        finally:
            await bad.aclose()
        assert health.status == "error"
        assert health.message


class TestIntrospection:
    async def test_relations_and_kinds(self, connector: MySQLConnector) -> None:
        by_name = {r.relation: r for r in await connector.introspect_schema()}
        assert sorted(by_name) == [
            "analytics.dim_customers",
            "analytics.fct_orders",
            "analytics.v_order_totals",
        ]
        assert by_name["analytics.fct_orders"].kind == "table"
        assert by_name["analytics.v_order_totals"].kind == "view"

    async def test_column_types(self, connector: MySQLConnector) -> None:
        by_name = {r.relation: r for r in await connector.introspect_schema()}
        customers = {c.name: c for c in by_name["analytics.dim_customers"].columns}
        orders = {c.name: c for c in by_name["analytics.fct_orders"].columns}

        assert customers["active"].type == "bool"
        assert customers["name"].type == "string"
        assert not customers["name"].nullable
        assert customers["created_at"].type == "timestamp"
        assert orders["order_id"].type == "int"
        assert orders["amount"].type == "decimal"
        assert orders["metadata"].type == "json"
        assert orders["order_date"].type == "date"

    async def test_keys(self, connector: MySQLConnector) -> None:
        by_name = {r.relation: r for r in await connector.introspect_schema()}
        orders = by_name["analytics.fct_orders"]
        assert orders.primary_key == ["order_id"]
        [fk] = orders.foreign_keys
        assert fk.columns == ["customer_id"]
        assert fk.references.relation == "analytics.dim_customers"
        assert fk.references.columns == ["customer_id"]

    async def test_system_schemas_are_not_listed(self, connector: MySQLConnector) -> None:
        schemas = {r.relation.split(".")[0] for r in await connector.introspect_schema()}
        assert schemas == {"analytics"}

    async def test_tables_filter(
        self, mysql_server: MySQLServer, analytics: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        narrowed = MySQLConnector(
            _connection(mysql_server, analytics, monkeypatch, tables=["fct_*"])
        )
        try:
            relations = [r.relation for r in await narrowed.introspect_schema()]
        finally:
            await narrowed.aclose()
        assert relations == ["analytics.fct_orders"]

    async def test_describe_relation(self, connector: MySQLConnector) -> None:
        columns = await connector.describe_relation("analytics.fct_orders")
        assert [c.name for c in columns] == [
            "order_id",
            "customer_id",
            "amount",
            "metadata",
            "order_date",
        ]
        assert [c.position for c in columns] == [1, 2, 3, 4, 5]

    async def test_describe_relation_in_the_default_database(
        self, connector: MySQLConnector
    ) -> None:
        assert [c.name for c in await connector.describe_relation("dim_customers")][
            0
        ] == "customer_id"


class TestRunReadOnlySql:
    async def test_rows_and_types(self, connector: MySQLConnector) -> None:
        result = await connector.run_read_only_sql(
            "SELECT customer_id, name, active FROM dim_customers ORDER BY customer_id"
        )
        assert [c.name for c in result.columns] == ["customer_id", "name", "active"]
        assert [c.type for c in result.columns] == ["int", "string", "int"]
        assert result.rows == [[1, "Ada", 1], [2, "Bo", 0]]
        assert not result.truncated

    async def test_row_limit_truncates(self, connector: MySQLConnector) -> None:
        result = await connector.run_read_only_sql("SELECT order_id FROM fct_orders ORDER BY 1")
        assert result.truncated
        assert [r[0] for r in result.rows] == [1, 2, 3, 4, 5]

    async def test_backticked_identifiers_and_percent_signs(
        self, connector: MySQLConnector
    ) -> None:
        result = await connector.run_read_only_sql(
            "SELECT `name` FROM `analytics`.`dim_customers` WHERE `name` LIKE 'A%'"
        )
        assert result.rows == [["Ada"]]

    @pytest.mark.parametrize(
        "sql", ["DELETE FROM fct_orders", "DROP TABLE fct_orders", "SELECT 1; SELECT 2"]
    )
    async def test_writes_are_refused_before_the_server_sees_them(
        self, connector: MySQLConnector, sql: str
    ) -> None:
        with pytest.raises(ReadOnlyViolation):
            await connector.run_read_only_sql(sql)
        count = await connector.run_read_only_sql("SELECT COUNT(*) FROM fct_orders")
        assert count.rows == [[7]]

    async def test_the_session_is_read_only_in_the_engine(self, connector: MySQLConnector) -> None:
        """Even if the parse guard were bypassed, MySQL refuses a write after our session setup."""

        def attempt_a_write(con: Any) -> None:
            connector._run_sql_sync(con, "SELECT 1")  # applies the session settings
            cursor = con.cursor()
            cursor.execute("DELETE FROM fct_orders")

        with pytest.raises(pymysql.err.OperationalError, match="READ ONLY"):
            await connector._in_session(attempt_a_write)
        count = await connector.run_read_only_sql("SELECT COUNT(*) FROM fct_orders")
        assert count.rows == [[7]]

    async def test_statement_timeout_stops_a_slow_select(self, connector: MySQLConnector) -> None:
        started = time.monotonic()
        with pytest.raises(pymysql.err.OperationalError, match="maximum statement execution time"):
            await connector.run_read_only_sql("SELECT SLEEP(10) FROM fct_orders")
        assert time.monotonic() - started < 5

    async def test_a_truncated_huge_result_returns_promptly(
        self, mysql_server: MySQLServer, analytics: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopping after ``row_limit`` rows must not read the rest of a very large result."""
        big = MySQLConnector(
            _connection(
                mysql_server, analytics, monkeypatch, row_limit=5, statement_timeout_ms=60_000
            )
        )
        # 7 rows joined with themselves 8 times, 5.7 million rows of about 100 bytes each.
        sql = "SELECT a.order_id, REPEAT('x', 100) AS pad FROM " + ", ".join(
            f"fct_orders {alias}" for alias in "abcdefgh"
        )
        try:
            started = time.monotonic()
            result = await big.run_read_only_sql(sql)
            elapsed = time.monotonic() - started
        finally:
            await big.aclose()
        assert result.truncated
        assert len(result.rows) == 5
        assert elapsed < 10, f"truncated result took {elapsed:.1f}s, the rest of the rows was read"


_DIALECT_DATABASE = "dialect_check"

_DIALECT_SEED = [
    "CREATE TABLE events (n INT PRIMARY KEY, d DATETIME, amount DECIMAL(10, 4))",
    "INSERT INTO events VALUES "
    "(1, '2025-03-13 15:45:00', 1.2345), "  # Thursday
    "(2, '2025-03-16 23:59:59', 2.5), "  # Sunday
    "(3, '2025-03-17 00:00:00', 3.75), "  # Monday
    "(4, '2025-11-30 08:00:00', 4.125), "  # Sunday, in Q4
    "(5, '2025-12-31 23:59:59', 5.5), "
    "(6, '2024-02-29 12:00:00', 6.25)",  # leap day
]


@pytest.fixture(scope="module")
def dialect_db(mysql_server: MySQLServer) -> str:
    mysql_server.new_database(_DIALECT_DATABASE)
    mysql_server.execute(_DIALECT_SEED, database=_DIALECT_DATABASE)
    return _DIALECT_DATABASE


@pytest.fixture
async def dialect_connector(
    mysql_server: MySQLServer, dialect_db: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[MySQLConnector]:
    yield MySQLConnector(_connection(mysql_server, dialect_db, monkeypatch))


class TestDialectAdapterOnMySQL:
    """The adapter's SQL has to mean the same thing on a real server, not just parse."""

    @pytest.mark.parametrize(
        ("unit", "n", "expected"),
        [
            ("day", 1, date(2025, 3, 13)),
            ("day", 5, date(2025, 12, 31)),
            ("week", 1, date(2025, 3, 10)),  # Thursday goes back to Monday
            ("week", 2, date(2025, 3, 10)),  # Sunday belongs to the week that started on Monday
            ("week", 3, date(2025, 3, 17)),  # Monday stays
            ("week", 6, date(2024, 2, 26)),
            ("month", 1, date(2025, 3, 1)),
            ("month", 6, date(2024, 2, 1)),
            ("quarter", 1, date(2025, 1, 1)),
            ("quarter", 4, date(2025, 10, 1)),
            ("quarter", 5, date(2025, 10, 1)),
            ("quarter", 6, date(2024, 1, 1)),
            ("year", 1, date(2025, 1, 1)),
            ("year", 6, date(2024, 1, 1)),
        ],
    )
    async def test_date_trunc_buckets(
        self, dialect_connector: MySQLConnector, unit: str, n: int, expected: date
    ) -> None:
        trunc = exp.func("DATE_TRUNC", exp.Literal.string(unit), exp.column("d", table="events"))
        query = exp.select(trunc.as_("bucket")).from_("events").where(f"events.n = {n}")
        result = await dialect_connector.run_read_only_sql(adapter_for("mysql").emit(query))
        assert result.rows == [[expected]]

    async def test_watermark_cast_compares_in_wall_clock_time(
        self, dialect_connector: MySQLConnector
    ) -> None:
        """Rows up to the watermark's own wall-clock time are in, later ones are out."""
        watermark = exp.Cast(
            this=exp.Literal.string("2025-03-16T23:59:59-04:00"),
            to=exp.DataType.build("TIMESTAMPTZ"),
        )
        query = (
            exp.select("n")
            .from_("events")
            .where(exp.LTE(this=exp.column("d", table="events"), expression=watermark))
            .order_by("n")
        )
        result = await dialect_connector.run_read_only_sql(adapter_for("mysql").emit(query))
        assert [r[0] for r in result.rows] == [1, 2, 6]

    async def test_bare_decimal_cast_keeps_the_fraction(
        self, dialect_connector: MySQLConnector
    ) -> None:
        cast = exp.Cast(this=exp.column("amount", table="events"), to=exp.DataType.build("NUMERIC"))
        query = exp.select(cast.as_("a")).from_("events").where("events.n = 1")
        result = await dialect_connector.run_read_only_sql(adapter_for("mysql").emit(query))
        assert result.rows == [[Decimal("1.2345")]]
