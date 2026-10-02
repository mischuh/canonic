"""MySQL connector unit tests, run against a fake PyMySQL so no server or driver is needed."""

from __future__ import annotations

import logging
import ssl
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

import canonic.connectors.mysql as mysql_module
from canonic.config import Connection
from canonic.connectors.base import Capability, ReadOnlyEnforcement
from canonic.connectors.factory import default_factory
from canonic.connectors.mysql import MySQLConnector
from canonic.exc import ConnectionError, CredentialError, ReadOnlyViolation

PASSWORD_ENV = "CANONIC_TEST_MYSQL_PASSWORD"


class FakeDriverError(Exception):
    """Stands in for a PyMySQL error."""


class FakeCursor:
    def __init__(self, con: FakeConnection, streaming: bool) -> None:
        self._con = con
        self.streaming = streaming
        self.closed = False
        self.description: list[tuple[str]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, args: tuple[Any, ...] | None = None) -> None:
        self._con.statements.append(sql)
        self._con.params.append(args)
        for needle, answer in self._con.answers:
            if needle in sql:
                if isinstance(answer, Exception):
                    raise answer
                rows, keys = answer
                self._rows = rows
                self.description = [(key,) for key in keys]
                return
        self._rows = []
        self.description = None  # like SET, which returns no result set

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self._rows[:size]

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """Answers each statement with the first canned answer whose needle occurs in its SQL."""

    def __init__(self, answers: list[tuple[str, Any]]) -> None:
        self.answers = answers
        self.statements: list[str] = []
        self.params: list[tuple[Any, ...] | None] = []
        self.cursors_opened: list[FakeCursor] = []
        self.closed = False

    def cursor(self, cls: type | None = None) -> FakeCursor:
        cursor = FakeCursor(self, streaming=cls is FakeDriver.cursors.SSCursor)
        self.cursors_opened.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class FakeDriver:
    """A stand-in for the ``pymysql`` module."""

    class cursors:  # noqa: N801 - mirrors pymysql.cursors
        class SSCursor:
            pass

    def __init__(self, answers: list[tuple[str, Any]] | None = None) -> None:
        self.answers = answers or []
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []
        self.connect_error: Exception | None = None

    def connect(self, **kwargs: Any) -> FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        self.connect_kwargs.append(kwargs)
        con = FakeConnection(self.answers)
        self.connections.append(con)
        return con


def _connection(**params: Any) -> Connection:
    return Connection(
        id="shop",
        type="mysql",
        params={"host": "db.example.com", "user": "reader", "database": "shop", **params},
        credentials_ref=f"env:{PASSWORD_ENV}",
    )


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "s3cret")


def _install(monkeypatch: pytest.MonkeyPatch, driver: FakeDriver) -> FakeDriver:
    """Swap the driver import so the connector talks to ``driver``."""
    monkeypatch.setattr(mysql_module, "_require_driver", lambda: driver)
    return driver


def _answers(**by_needle: Any) -> list[tuple[str, Any]]:
    return list(by_needle.items())


class TestConstruction:
    def test_registered_in_default_factory(self) -> None:
        assert "mysql" in default_factory.registered_types()
        assert isinstance(default_factory.create(_connection()), MySQLConnector)

    def test_missing_credentials_ref_is_a_credential_error(self) -> None:
        connection = Connection(id="shop", type="mysql", params={"host": "h", "user": "u"})
        with pytest.raises(CredentialError):
            MySQLConnector(connection)

    def test_construction_needs_no_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_driver(name: str) -> Any:
            raise AssertionError(f"driver {name} imported during construction")

        monkeypatch.setattr(mysql_module.importlib, "import_module", no_driver)
        MySQLConnector(_connection())

    def test_fetch_column_stats_is_ignored_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            MySQLConnector(_connection(fetch_column_stats=True))
        assert "fetch_column_stats" in caplog.text

    def test_capabilities(self) -> None:
        caps = MySQLConnector(_connection()).capabilities()
        assert Capability.RUN_READ_ONLY_SQL in caps
        assert Capability.INTROSPECT_SCHEMA in caps


class TestConnect:
    async def test_connect_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver())
        await MySQLConnector(_connection(statement_timeout_ms=5000)).test_connection()

        [kwargs] = driver.connect_kwargs
        assert kwargs["host"] == "db.example.com"
        assert kwargs["port"] == 3306
        assert kwargs["user"] == "reader"
        assert kwargs["password"] == "s3cret"
        assert kwargs["database"] == "shop"
        assert kwargs["charset"] == "utf8mb4"
        assert kwargs["autocommit"] is True
        assert kwargs["ssl"] is None
        assert kwargs["read_timeout"] == 5 + mysql_module._READ_TIMEOUT_GRACE_S

    async def test_dbname_is_an_alias_and_database_is_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver())
        alias = Connection(
            id="a",
            type="mysql",
            params={"host": "h", "user": "u", "dbname": "other", "port": 3307},
            credentials_ref=f"env:{PASSWORD_ENV}",
        )
        bare = Connection(
            id="b",
            type="mysql",
            params={"host": "h", "user": "u"},
            credentials_ref=f"env:{PASSWORD_ENV}",
        )
        await MySQLConnector(alias).test_connection()
        await MySQLConnector(bare).test_connection()

        assert (driver.connect_kwargs[0]["database"], driver.connect_kwargs[0]["port"]) == (
            "other",
            3307,
        )
        assert driver.connect_kwargs[1]["database"] is None

    async def test_ssl_builds_a_verifying_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver())
        await MySQLConnector(_connection(ssl=True)).test_connection()

        context = driver.connect_kwargs[0]["ssl"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname

    async def test_every_session_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver(_answers(**{"VERSION()": ([("8.4.2",)], ["v"])})))
        connector = MySQLConnector(_connection())
        await connector.test_connection()
        await connector.introspect_schema()
        assert driver.connections
        assert all(con.closed for con in driver.connections)

    async def test_session_is_closed_when_the_query_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver(_answers(**{"FROM t": FakeDriverError("boom")})))
        with pytest.raises(FakeDriverError):
            await MySQLConnector(_connection()).run_read_only_sql("SELECT * FROM t")
        assert driver.connections[0].closed


class TestDriver:
    def test_missing_driver_gives_an_install_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(name: str) -> Any:
            raise ImportError(name)

        monkeypatch.setattr(mysql_module.importlib, "import_module", missing)
        with pytest.raises(ConnectionError, match=r"canonic\[mysql\]"):
            MySQLConnector(_connection())._connect()

    async def test_connect_failure_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver())
        driver.connect_error = FakeDriverError("Access denied for user 'reader'")
        health = await MySQLConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "Access denied" in (health.message or "")

    async def test_missing_driver_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing(name: str) -> Any:
            raise ImportError(name)

        monkeypatch.setattr(mysql_module.importlib, "import_module", missing)
        health = await MySQLConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "PyMySQL" in (health.message or "")


class TestTestConnection:
    @pytest.mark.parametrize("version", ["8.0.36", "8.4.2", "9.1.0", "10.11.6-MariaDB"])
    async def test_supported_versions_are_ok(
        self, monkeypatch: pytest.MonkeyPatch, version: str
    ) -> None:
        _install(monkeypatch, FakeDriver(_answers(**{"VERSION()": ([(version,)], ["v"])})))
        assert (await MySQLConnector(_connection()).test_connection()).status == "ok"

    async def test_old_server_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakeDriver(_answers(**{"VERSION()": ([("5.7.44-log",)], ["v"])})))
        health = await MySQLConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "8.0" in (health.message or "")
        assert "5.7.44-log" in (health.message or "")

    async def test_version_returned_as_bytes_is_decoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeDriver(_answers(**{"VERSION()": ([(b"8.4.2",)], ["v"])})))
        assert (await MySQLConnector(_connection()).test_connection()).status == "ok"


class TestReadOnly:
    def test_enforcement_is_native(self) -> None:
        assert MySQLConnector(_connection()).read_only_enforcement() is ReadOnlyEnforcement.NATIVE

    @pytest.mark.parametrize("sql", ["DELETE FROM t", "DROP TABLE t", "SELECT 1; SELECT 2"])
    async def test_writes_are_refused_before_connecting(
        self, monkeypatch: pytest.MonkeyPatch, sql: str
    ) -> None:
        driver = _install(monkeypatch, FakeDriver())
        with pytest.raises(ReadOnlyViolation):
            await MySQLConnector(_connection()).run_read_only_sql(sql)
        assert driver.connections == []

    async def test_session_is_read_only_with_a_timeout_before_the_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver(_answers(**{"FROM `t`": ([(1,)], ["a"])})))
        await MySQLConnector(_connection(statement_timeout_ms=1500)).run_read_only_sql(
            "SELECT a FROM `t`"
        )
        [con] = driver.connections
        assert con.statements == [
            "SET SESSION TRANSACTION READ ONLY",
            "SET SESSION max_execution_time = 1500",
            "SELECT a FROM `t`",
        ]


class TestRunReadOnlySql:
    async def test_rows_and_inferred_column_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [(1, "a", Decimal("1.5"), date(2025, 1, 1)), (2, "b", None, None)]
        _install(
            monkeypatch,
            FakeDriver(_answers(**{"FROM t": (rows, ["id", "name", "amount", "day"])})),
        )
        result = await MySQLConnector(_connection()).run_read_only_sql("SELECT * FROM t")
        assert [(c.name, c.type) for c in result.columns] == [
            ("id", "int"),
            ("name", "string"),
            ("amount", "decimal"),
            ("day", "date"),
        ]
        assert result.rows[1] == [2, "b", None, None]
        assert not result.truncated

    async def test_the_query_runs_on_an_unbuffered_cursor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver(_answers(**{"FROM t": ([(1,)], ["n"])})))
        await MySQLConnector(_connection()).run_read_only_sql("SELECT n FROM t")
        setup, query = driver.connections[0].cursors_opened
        assert not setup.streaming
        assert query.streaming

    async def test_row_limit_truncates_and_leaves_the_cursor_unread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(
            monkeypatch,
            FakeDriver(_answers(**{"FROM t": ([(i,) for i in range(5)], ["n"])})),
        )
        result = await MySQLConnector(_connection(row_limit=3)).run_read_only_sql("SELECT n FROM t")

        assert result.truncated
        assert [r[0] for r in result.rows] == [0, 1, 2]
        [con] = driver.connections
        query = con.cursors_opened[-1]
        # Closing the cursor would read the rest of the result, so only the connection is closed.
        assert not query.closed
        assert con.closed

    async def test_exactly_the_row_limit_is_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(
            monkeypatch,
            FakeDriver(_answers(**{"FROM t": ([(i,) for i in range(3)], ["n"])})),
        )
        result = await MySQLConnector(_connection(row_limit=3)).run_read_only_sql("SELECT n FROM t")
        assert not result.truncated
        assert driver.connections[0].cursors_opened[-1].closed

    async def test_statement_without_a_result_set_returns_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeDriver())
        result = await MySQLConnector(_connection()).run_read_only_sql("SELECT 1")
        assert result.columns == []
        assert result.rows == []


_TABLES = [
    ("shop", "customers", "BASE TABLE"),
    ("shop", "orders", "BASE TABLE"),
    ("shop", "order_totals", "VIEW"),
    ("shop", "legacy", "SYSTEM VIEW"),
    ("other", "events", "BASE TABLE"),
]

_COLUMNS = [
    ("shop", "customers", "id", "int", "int", "NO", 1),
    ("shop", "customers", "active", "tinyint", "tinyint(1)", "NO", 2),
    ("shop", "orders", "id", "bigint", "bigint", "NO", 1),
    ("shop", "orders", "customer_id", "int", "int", "NO", 2),
    ("shop", "orders", "total", "decimal", "decimal(10,2)", "YES", 3),
    ("shop", "orders", "placed_at", "datetime", "datetime", "YES", 4),
    ("shop", "orders", "note", "text", "text", "YES", 5),
    ("shop", "orders", "meta", "json", "json", "YES", 6),
    ("shop", "orders", "blob", "blob", "blob", "YES", 7),
    ("shop", "order_totals", "total", "decimal", "decimal(10,2)", "YES", 1),
    ("shop", "legacy", "x", "int", "int", "YES", 1),
    ("other", "events", "id", "int", "int", "NO", 1),
]

_PRIMARY_KEYS = [
    ("shop", "customers", "id"),
    ("shop", "orders", "id"),
    ("other", "events", "id"),
]

_FOREIGN_KEYS = [
    ("shop", "orders", "fk_orders_customer", "customer_id", "shop", "customers", "id"),
]

_ESTIMATES = [
    ("shop", "customers", 120),
    ("shop", "orders", 5000),
    ("shop", "order_totals", None),
    ("other", "events", 7),
]


def _catalog(**overrides: Any) -> list[tuple[str, Any]]:
    answers: dict[str, Any] = {
        "table_type": (_TABLES, []),
        "table_rows": (_ESTIMATES, []),
        "table_name = %s": ([], []),
        "information_schema.columns": (_COLUMNS, []),
        "constraint_name = 'PRIMARY'": (_PRIMARY_KEYS, []),
        "referenced_table_name IS NOT NULL": (_FOREIGN_KEYS, []),
    }
    answers.update(overrides)
    return list(answers.items())


async def _introspect(monkeypatch: pytest.MonkeyPatch, **params: Any) -> dict[str, Any]:
    _install(monkeypatch, FakeDriver(_catalog()))
    relations = await MySQLConnector(_connection(**params)).introspect_schema()
    return {r.relation: r for r in relations}


class TestIntrospection:
    async def test_relations_kinds_and_estimates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        by_name = await _introspect(monkeypatch)

        assert sorted(by_name) == [
            "other.events",
            "shop.customers",
            "shop.order_totals",
            "shop.orders",
        ]
        assert by_name["shop.orders"].kind == "table"
        assert by_name["shop.order_totals"].kind == "view"
        assert by_name["shop.orders"].row_count_estimate == 5000
        assert by_name["shop.order_totals"].row_count_estimate is None
        assert by_name["shop.orders"].connection == "shop"

    async def test_system_views_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "shop.legacy" not in await _introspect(monkeypatch)

    async def test_system_schemas_are_excluded_in_every_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver(_catalog()))
        await MySQLConnector(_connection()).introspect_schema()
        [con] = driver.connections
        assert len(con.statements) == 5
        assert all("'performance_schema'" in sql for sql in con.statements)

    async def test_columns_types_and_nullability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        by_name = await _introspect(monkeypatch)

        customers = {c.name: c for c in by_name["shop.customers"].columns}
        assert customers["active"].type == "bool"  # tinyint(1) is MySQL's boolean
        assert customers["id"].type == "int"
        assert not customers["id"].nullable

        orders = {c.name: c for c in by_name["shop.orders"].columns}
        assert orders["total"].type == "decimal"
        assert orders["total"].nullable
        assert orders["placed_at"].type == "timestamp"
        assert orders["note"].type == "string"
        assert orders["meta"].type == "json"
        assert [c.position for c in by_name["shop.orders"].columns] == list(range(1, 8))

    async def test_unmapped_type_is_recorded_as_json_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            by_name = await _introspect(monkeypatch)
        blob = next(c for c in by_name["shop.orders"].columns if c.name == "blob")
        assert blob.type == "json"
        assert "blob" in caplog.text

    async def test_primary_and_foreign_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        by_name = await _introspect(monkeypatch)

        assert by_name["shop.orders"].primary_key == ["id"]
        assert by_name["shop.order_totals"].primary_key == []
        [fk] = by_name["shop.orders"].foreign_keys
        assert fk.columns == ["customer_id"]
        assert fk.references.relation == "shop.customers"
        assert fk.references.columns == ["id"]

    async def test_composite_foreign_key_keeps_column_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        composite = [
            ("shop", "orders", "fk_pair", "a", "shop", "customers", "x"),
            ("shop", "orders", "fk_pair", "b", "shop", "customers", "y"),
        ]
        _install(
            monkeypatch,
            FakeDriver(_catalog(**{"referenced_table_name IS NOT NULL": (composite, [])})),
        )
        relations = await MySQLConnector(_connection()).introspect_schema()
        orders = next(r for r in relations if r.relation == "shop.orders")
        [fk] = orders.foreign_keys
        assert fk.columns == ["a", "b"]
        assert fk.references.columns == ["x", "y"]

    async def test_schemas_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert sorted(await _introspect(monkeypatch, schemas=["other"])) == ["other.events"]

    async def test_tables_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert sorted(await _introspect(monkeypatch, tables=["order*"])) == [
            "shop.order_totals",
            "shop.orders",
        ]

    async def test_metadata_returned_as_bytes_is_decoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = _catalog(
            **{
                "table_type": ([(b"shop", b"t", b"BASE TABLE")], []),
                "table_rows": ([(b"shop", b"t", 3)], []),
                "information_schema.columns": (
                    [(b"shop", b"t", b"id", b"int", b"int", b"NO", 1)],
                    [],
                ),
                "constraint_name = 'PRIMARY'": ([], []),
                "referenced_table_name IS NOT NULL": ([], []),
            }
        )
        _install(monkeypatch, FakeDriver(answers))
        [relation] = await MySQLConnector(_connection()).introspect_schema()
        assert relation.relation == "shop.t"
        assert relation.columns[0].name == "id"

    async def test_fingerprint_changes_with_the_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = await _introspect(monkeypatch)
        _install(
            monkeypatch,
            FakeDriver(
                _catalog(
                    **{
                        "information_schema.columns": (
                            [row for row in _COLUMNS if row[2] != "note"],
                            [],
                        )
                    }
                )
            ),
        )
        second = {r.relation: r for r in await MySQLConnector(_connection()).introspect_schema()}
        assert first["shop.orders"].source_fingerprint != second["shop.orders"].source_fingerprint


class TestDescribeRelation:
    async def test_describes_with_bound_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [("id", "int", "int", "NO", 1), ("flag", "tinyint", "tinyint(1)", "YES", 2)]
        driver = _install(monkeypatch, FakeDriver(_answers(**{"table_name = %s": (rows, [])})))
        columns = await MySQLConnector(_connection()).describe_relation("shop.orders")

        assert [(c.name, c.type, c.nullable, c.position) for c in columns] == [
            ("id", "int", False, 1),
            ("flag", "bool", True, 2),
        ]
        assert driver.connections[0].params == [("shop", "orders")]

    async def test_bare_table_uses_the_default_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [("id", "int", "int", "NO", 1)]
        driver = _install(monkeypatch, FakeDriver(_answers(**{"table_name = %s": (rows, [])})))
        await MySQLConnector(_connection()).describe_relation("orders")
        assert driver.connections[0].params == [(None, "orders")]

    async def test_unknown_relation_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakeDriver())
        with pytest.raises(ValueError, match="not found"):
            await MySQLConnector(_connection()).describe_relation("shop.nope")

    @pytest.mark.parametrize("relation", ["a.b.c", "shop."])
    async def test_malformed_relation_raises(
        self, monkeypatch: pytest.MonkeyPatch, relation: str
    ) -> None:
        driver = _install(monkeypatch, FakeDriver())
        with pytest.raises(ValueError, match="unsafe"):
            await MySQLConnector(_connection()).describe_relation(relation)
        assert driver.connections == []
