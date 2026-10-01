"""Tests for the Snowflake connector (SPEC-E2 §6).

Unit tests drive the connector through a fake driver injected in place of
``snowflake.connector``, so they need neither the driver package nor an account. The live
test at the bottom is marked ``integration`` and skipped unless ``CANONIC_TEST_SNOWFLAKE_*``
is set.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from typing import Any

import pytest

from canonic.config import Connection
from canonic.connectors import snowflake as sf_module
from canonic.connectors.base import AcquisitionTier, Capability
from canonic.connectors.factory import default_factory
from canonic.connectors.snowflake import SnowflakeConnector, _normalize_type
from canonic.exc import ConnectionError, ReadOnlyViolation


class FakeDriverError(Exception):
    """Stands in for ``snowflake.connector.Error``."""


class FakeCursor:
    """Answers queries from ``results``, keyed by a substring of the SQL.

    A query containing any ``fail_on`` needle raises :class:`FakeDriverError`.
    """

    def __init__(
        self,
        results: dict[str, tuple[list[str], list[tuple[Any, ...]]]],
        fail_on: tuple[str, ...] = (),
    ) -> None:
        self._results = results
        self._fail_on = fail_on
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] | None = None
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if any(needle in sql for needle in self._fail_on):
            raise FakeDriverError(f"SQL access control error: {sql}")
        for needle, (names, rows) in self._results.items():
            if needle in sql:
                self.description = [(n,) for n in names]
                self._rows = rows
                return
        self.description = None
        self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return self._rows[:size]

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(
        self,
        results: dict[str, tuple[list[str], list[tuple[Any, ...]]]],
        fail_on: tuple[str, ...] = (),
    ) -> None:
        self._results = results
        self._fail_on = fail_on
        self.cursors: list[FakeCursor] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        cursor = FakeCursor(self._results, self._fail_on)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class FakeDriver:
    """Stands in for the ``snowflake.connector`` module."""

    Error = FakeDriverError

    def __init__(
        self,
        results: dict[str, tuple[list[str], list[tuple[Any, ...]]]],
        fail_on: tuple[str, ...] = (),
    ) -> None:
        self._results = results
        self._fail_on = fail_on
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connect_kwargs.append(kwargs)
        con = FakeConnection(self._results, self._fail_on)
        self.connections.append(con)
        return con


_INTROSPECTION_RESULTS: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {
    "INFORMATION_SCHEMA.TABLES": (
        ["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE", "ROW_COUNT"],
        [
            ("PUBLIC", "ORDERS", "BASE TABLE", 120),
            ("PUBLIC", "CUSTOMERS", "BASE TABLE", None),
            ("PUBLIC", "V_ORDERS", "VIEW", None),
            ("PUBLIC", "STAGE_TMP", "EXTERNAL TABLE", None),
            ("RAW", "EVENTS", "BASE TABLE", 5),
        ],
    ),
    "INFORMATION_SCHEMA.COLUMNS": (
        [
            "TABLE_SCHEMA",
            "TABLE_NAME",
            "COLUMN_NAME",
            "DATA_TYPE",
            "IS_NULLABLE",
            "ORDINAL_POSITION",
            "NUMERIC_SCALE",
        ],
        [
            ("PUBLIC", "ORDERS", "ID", "NUMBER", "NO", 1, 0),
            ("PUBLIC", "ORDERS", "CUSTOMER_ID", "NUMBER", "YES", 2, 0),
            ("PUBLIC", "ORDERS", "AMOUNT", "NUMBER", "YES", 3, 2),
            ("PUBLIC", "ORDERS", "CREATED_AT", "TIMESTAMP_NTZ", "YES", 4, None),
            ("PUBLIC", "CUSTOMERS", "ID", "NUMBER", "NO", 1, 0),
            ("PUBLIC", "CUSTOMERS", "NAME", "TEXT", "YES", 2, None),
            ("PUBLIC", "V_ORDERS", "ID", "NUMBER", "YES", 1, 0),
            ("RAW", "EVENTS", "PAYLOAD", "VARIANT", "YES", 1, None),
        ],
    ),
    "SHOW PRIMARY KEYS": (
        ["schema_name", "table_name", "column_name", "key_sequence"],
        [
            ("PUBLIC", "ORDERS", "ID", 1),
            ("PUBLIC", "CUSTOMERS", "ID", 1),
        ],
    ),
    "SHOW IMPORTED KEYS": (
        [
            "pk_schema_name",
            "pk_table_name",
            "pk_column_name",
            "fk_schema_name",
            "fk_table_name",
            "fk_column_name",
            "key_sequence",
            "fk_name",
        ],
        [("PUBLIC", "CUSTOMERS", "ID", "PUBLIC", "ORDERS", "CUSTOMER_ID", 1, "FK_ORDERS_CUST")],
    ),
}


def _connection(**overrides: Any) -> Connection:
    params: dict[str, Any] = {
        "account": "xy12345.eu-central-1",
        "user": "CANONIC",
        "warehouse": "WH",
        "database": "ANALYTICS",
    }
    params.update(overrides.pop("params", {}))
    return Connection(
        id="wh",
        type="snowflake",
        params=params,
        credentials_ref=overrides.pop("credentials_ref", "env:SF_TEST_PASSWORD"),
        **overrides,
    )


@pytest.fixture
def password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SF_TEST_PASSWORD", "s3cret")


def _install(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[str, tuple[list[str], list[tuple[Any, ...]]]] | None = None,
    fail_on: tuple[str, ...] = (),
) -> FakeDriver:
    driver = FakeDriver(results if results is not None else _INTROSPECTION_RESULTS, fail_on)
    monkeypatch.setattr(sf_module, "_require_driver", lambda: driver)
    return driver


class TestConstruction:
    def test_registered_in_default_factory(self, password: None) -> None:
        assert "snowflake" in default_factory.registered_types()
        assert isinstance(default_factory.create(_connection()), SnowflakeConnector)

    def test_missing_account_is_a_config_error(self, password: None) -> None:
        conn = Connection(
            id="wh", type="snowflake", params={"user": "u"}, credentials_ref="env:SF_TEST_PASSWORD"
        )
        with pytest.raises(ConnectionError, match="account"):
            SnowflakeConnector(conn)

    def test_capabilities(self, password: None) -> None:
        caps = SnowflakeConnector(_connection()).capabilities()
        assert Capability.RUN_READ_ONLY_SQL in caps
        assert Capability.INTROSPECT_SCHEMA in caps

    async def test_missing_driver_gives_install_hint(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = importlib.import_module

        def _missing(name: str, package: str | None = None) -> Any:
            if name.startswith("snowflake"):
                raise ImportError(name)
            return real_import(name, package)

        monkeypatch.setattr(importlib, "import_module", _missing)
        health = await SnowflakeConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "pip install snowflake-connector-python" in (health.message or "")


class TestConnect:
    async def test_session_parameters_and_role(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch)
        connector = SnowflakeConnector(
            _connection(read_only_role="CANONIC_RO", params={"statement_timeout_ms": 45_500})
        )
        health = await connector.test_connection()

        assert health.status == "ok"
        kwargs = driver.connect_kwargs[0]
        assert kwargs["account"] == "xy12345.eu-central-1"
        assert kwargs["password"] == "s3cret"
        assert kwargs["role"] == "CANONIC_RO"
        assert kwargs["warehouse"] == "WH"
        assert kwargs["session_parameters"] == {
            "STATEMENT_TIMEOUT_IN_SECONDS": 46,
            "QUERY_TAG": "canonic",
        }
        assert driver.connections[0].closed

    async def test_read_only_role_wins_over_params_role(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch)
        connector = SnowflakeConnector(
            _connection(read_only_role="RO", params={"role": "ACCOUNTADMIN"})
        )
        await connector.test_connection()
        assert driver.connect_kwargs[0]["role"] == "RO"

    async def test_connect_failure_is_reported_not_raised(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            def connect(self, **_: Any) -> None:
                raise RuntimeError("bad credentials")

        monkeypatch.setattr(sf_module, "_require_driver", lambda: _Boom())
        health = await SnowflakeConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "bad credentials" in (health.message or "")


class TestIntrospection:
    async def test_relations_columns_keys(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        schemas = await SnowflakeConnector(_connection()).introspect_schema()
        by_rel = {s.relation: s for s in schemas}

        # EXTERNAL TABLE is skipped, the other four relations are kept
        assert set(by_rel) == {
            "PUBLIC.ORDERS",
            "PUBLIC.CUSTOMERS",
            "PUBLIC.V_ORDERS",
            "RAW.EVENTS",
        }
        orders = by_rel["PUBLIC.ORDERS"]
        assert orders.connection == "wh"
        assert orders.kind == "table"
        assert orders.acquisition_tier == AcquisitionTier.LIVE
        assert orders.row_count_estimate == 120
        assert orders.primary_key == ["ID"]
        assert [(c.name, c.type) for c in orders.columns] == [
            ("ID", "int"),
            ("CUSTOMER_ID", "int"),
            ("AMOUNT", "decimal"),
            ("CREATED_AT", "timestamp"),
        ]
        assert [c.nullable for c in orders.columns] == [False, True, True, True]
        assert len(orders.foreign_keys) == 1
        fk = orders.foreign_keys[0]
        assert fk.columns == ["CUSTOMER_ID"]
        assert fk.references.relation == "PUBLIC.CUSTOMERS"
        assert fk.references.columns == ["ID"]
        assert by_rel["PUBLIC.V_ORDERS"].kind == "view"
        assert by_rel["RAW.EVENTS"].columns[0].type == "json"

    async def test_key_query_failure_degrades_to_no_keys(
        self,
        password: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _install(monkeypatch, fail_on=("SHOW PRIMARY KEYS", "SHOW IMPORTED KEYS"))
        schemas = await SnowflakeConnector(_connection()).introspect_schema()
        orders = next(s for s in schemas if s.relation == "PUBLIC.ORDERS")

        assert [c.name for c in orders.columns] == ["ID", "CUSTOMER_ID", "AMOUNT", "CREATED_AT"]
        assert orders.primary_key == []
        assert orders.foreign_keys == []
        assert "could not read Snowflake primary keys" in caplog.text
        assert "could not read Snowflake foreign keys" in caplog.text

    async def test_non_driver_error_in_key_query_still_propagates(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Cursor(FakeCursor):
            def execute(self, sql: str) -> None:
                if "SHOW PRIMARY KEYS" in sql:
                    raise RuntimeError("bug, not a driver error")
                super().execute(sql)

        driver = _install(monkeypatch)
        original = driver.connect

        def connect(**kwargs: Any) -> FakeConnection:
            con = original(**kwargs)
            con.cursor = lambda: _Cursor(_INTROSPECTION_RESULTS)  # type: ignore[method-assign]
            return con

        driver.connect = connect  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="not a driver error"):
            await SnowflakeConnector(_connection()).introspect_schema()

    async def test_schema_and_table_filters(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        connector = SnowflakeConnector(
            _connection(params={"schemas": ["PUBLIC"], "tables": ["O*"]})
        )
        relations = {s.relation for s in await connector.introspect_schema()}
        assert relations == {"PUBLIC.ORDERS"}

    async def test_introspection_requires_database(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        conn = Connection(
            id="wh",
            type="snowflake",
            params={"account": "a", "user": "u"},
            credentials_ref="env:SF_TEST_PASSWORD",
        )
        with pytest.raises(ConnectionError, match="params.database"):
            await SnowflakeConnector(conn).introspect_schema()

    async def test_database_name_is_quoted(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch)
        connector = SnowflakeConnector(_connection(params={"database": 'EVIL"; DROP'}))
        await connector.introspect_schema()
        executed = [sql for cur in driver.connections[0].cursors for sql in cur.executed]
        assert any('"EVIL""; DROP".INFORMATION_SCHEMA.TABLES' in sql for sql in executed)


class TestRunReadOnlySql:
    async def test_returns_rows_and_types(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            {"SELECT": (["N", "TOTAL"], [(1, Decimal("2.50")), (2, Decimal("3.00"))])},
        )
        result = await SnowflakeConnector(_connection()).run_read_only_sql("SELECT 1")
        assert [(c.name, c.type) for c in result.columns] == [("N", "int"), ("TOTAL", "decimal")]
        assert result.rows == [[1, Decimal("2.50")], [2, Decimal("3.00")]]
        assert not result.truncated

    async def test_truncates_at_row_limit(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, {"SELECT": (["N"], [(i,) for i in range(5)])})
        connector = SnowflakeConnector(_connection(params={"row_limit": 3}))
        result = await connector.run_read_only_sql("SELECT n FROM t")
        assert result.truncated
        assert len(result.rows) == 3

    @pytest.mark.parametrize("sql", ["DELETE FROM t", "SELECT 1; SELECT 2", "DROP TABLE t"])
    async def test_rejects_writes_before_connecting(
        self, sql: str, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch)
        with pytest.raises(ReadOnlyViolation):
            await SnowflakeConnector(_connection()).run_read_only_sql(sql)
        assert driver.connect_kwargs == []

    async def test_accepts_snowflake_only_syntax(
        self, password: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, {"payload:user": (["UID"], [("u1",)])})
        sql = "SELECT payload:user.id::string AS uid FROM events"
        result = await SnowflakeConnector(_connection()).run_read_only_sql(sql)
        assert result.rows == [["u1"]]


class TestDescribeRelation:
    async def test_describe_table(self, password: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            {
                "DESC TABLE": (
                    ["name", "type", "null?"],
                    [
                        ("ID", "NUMBER(38,0)", "N"),
                        ("AMOUNT", "NUMBER(18,2)", "Y"),
                        ("NAME", "VARCHAR(16777216)", "Y"),
                    ],
                )
            },
        )
        cols = await SnowflakeConnector(_connection()).describe_relation("PUBLIC.ORDERS")
        assert [(c.name, c.type, c.nullable) for c in cols] == [
            ("ID", "int", False),
            ("AMOUNT", "decimal", True),
            ("NAME", "string", True),
        ]

    async def test_unsafe_relation_rejected(self, password: None) -> None:
        with pytest.raises(ValueError, match="unsafe relation"):
            await SnowflakeConnector(_connection()).describe_relation("t; DROP TABLE x")


class TestNormalizeType:
    @pytest.mark.parametrize(
        ("raw", "scale", "expected"),
        [
            ("NUMBER", 0, "int"),
            ("NUMBER", 2, "decimal"),
            ("NUMBER(38,0)", None, "int"),
            ("NUMBER(18,4)", None, "decimal"),
            ("FLOAT", None, "float"),
            ("TEXT", None, "string"),
            ("VARCHAR(100)", None, "string"),
            ("BOOLEAN", None, "bool"),
            ("DATE", None, "date"),
            ("TIMESTAMP_TZ", None, "timestamp"),
            ("TIMESTAMP_LTZ(9)", None, "timestamp"),
            ("TIME", None, "string"),
            ("VARIANT", None, "json"),
            ("ARRAY", None, "json"),
            ("SOMETHING_NEW", None, "json"),
        ],
    )
    def test_mapping(self, raw: str, scale: int | None, expected: str) -> None:
        assert _normalize_type(raw, "S.T", "C", scale) == expected
