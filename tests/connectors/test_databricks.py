"""Tests for the Databricks connector (SPEC-E2 §6).

Unit tests drive the connector through a fake driver injected in place of ``databricks.sql``,
so they need neither the driver package nor a workspace. The live test is in
``test_databricks_live.py``.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from typing import Any

import pytest

from canonic.config import Connection
from canonic.connectors import databricks as dbx_module
from canonic.connectors.base import AcquisitionTier, Capability, ReadOnlyEnforcement
from canonic.connectors.databricks import DatabricksConnector, _normalize_type
from canonic.connectors.factory import default_factory
from canonic.exc import ConnectionError, ReadOnlyViolation

Results = dict[str, tuple[list[str], list[tuple[Any, ...]]]]


class FakeDriverError(Exception):
    """Stands in for ``databricks.sql.Error``."""


class FakeCursor:
    """Answers queries from ``results``, keyed by a substring of the SQL.

    A query containing any ``fail_on`` needle raises :class:`FakeDriverError`.
    """

    def __init__(self, results: Results, fail_on: tuple[str, ...] = ()) -> None:
        self._results = results
        self._fail_on = fail_on
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str]] | None = None
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if any(needle in sql for needle in self._fail_on):
            raise FakeDriverError(f"PERMISSION_DENIED: {sql}")
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
    def __init__(self, results: Results, fail_on: tuple[str, ...] = ()) -> None:
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
    """Stands in for the ``databricks.sql`` module."""

    Error = FakeDriverError

    def __init__(self, results: Results, fail_on: tuple[str, ...] = ()) -> None:
        self._results = results
        self._fail_on = fail_on
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connections: list[FakeConnection] = []

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connect_kwargs.append(kwargs)
        con = FakeConnection(self._results, self._fail_on)
        self.connections.append(con)
        return con


_INTROSPECTION_RESULTS: Results = {
    "information_schema.tables": (
        ["TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"],
        [
            ("shop", "orders", "MANAGED"),
            ("shop", "customers", "MANAGED"),
            ("shop", "v_orders", "VIEW"),
            ("shop", "mv_daily", "MATERIALIZED_VIEW"),
            ("raw", "events", "EXTERNAL"),
        ],
    ),
    "information_schema.columns": (
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
            ("shop", "orders", "id", "LONG", "NO", 1, 0),
            ("shop", "orders", "customer_id", "LONG", "YES", 2, 0),
            ("shop", "orders", "amount", "DECIMAL", "YES", 3, 2),
            ("shop", "orders", "created_at", "TIMESTAMP", "YES", 4, None),
            ("shop", "customers", "id", "LONG", "NO", 1, 0),
            ("shop", "customers", "name", "STRING", "YES", 2, None),
            ("shop", "v_orders", "id", "LONG", "YES", 1, 0),
            ("shop", "mv_daily", "day", "DATE", "YES", 1, None),
            ("raw", "events", "payload", "STRUCT", "YES", 1, None),
        ],
    ),
    "key_column_usage": (
        [
            "CONSTRAINT_SCHEMA",
            "CONSTRAINT_NAME",
            "CONSTRAINT_TYPE",
            "TABLE_SCHEMA",
            "TABLE_NAME",
            "COLUMN_NAME",
            "ORDINAL_POSITION",
        ],
        [
            ("shop", "pk_customers", "PRIMARY KEY", "shop", "customers", "id", 1),
            ("shop", "fk_orders_customer", "FOREIGN KEY", "shop", "orders", "customer_id", 1),
            ("shop", "pk_orders", "PRIMARY KEY", "shop", "orders", "id", 1),
        ],
    ),
    "referential_constraints": (
        [
            "CONSTRAINT_SCHEMA",
            "CONSTRAINT_NAME",
            "UNIQUE_CONSTRAINT_SCHEMA",
            "UNIQUE_CONSTRAINT_NAME",
        ],
        [("shop", "fk_orders_customer", "shop", "pk_customers")],
    ),
}


def _connection(**overrides: Any) -> Connection:
    params: dict[str, Any] = {
        "server_hostname": "dbc-1234.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123",
        "catalog": "analytics",
    }
    params.update(overrides.pop("params", {}))
    return Connection(
        id="wh",
        type="databricks",
        params=params,
        credentials_ref=overrides.pop("credentials_ref", "env:DBX_TEST_TOKEN"),
        **overrides,
    )


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBX_TEST_TOKEN", "dapi-secret")


def _install(
    monkeypatch: pytest.MonkeyPatch,
    results: Results | None = None,
    fail_on: tuple[str, ...] = (),
) -> FakeDriver:
    driver = FakeDriver(results if results is not None else _INTROSPECTION_RESULTS, fail_on)
    monkeypatch.setattr(dbx_module, "_require_driver", lambda: driver)
    return driver


class TestConstruction:
    def test_registered_in_default_factory(self, token: None) -> None:
        assert "databricks" in default_factory.registered_types()
        assert isinstance(default_factory.create(_connection()), DatabricksConnector)

    @pytest.mark.parametrize("missing", ["server_hostname", "http_path"])
    def test_missing_required_param_is_a_config_error(self, token: None, missing: str) -> None:
        conn = _connection()
        del conn.params[missing]
        with pytest.raises(ConnectionError, match=missing):
            DatabricksConnector(conn)

    def test_capabilities(self, token: None) -> None:
        caps = DatabricksConnector(_connection()).capabilities()
        assert Capability.RUN_READ_ONLY_SQL in caps
        assert Capability.INTROSPECT_SCHEMA in caps

    async def test_missing_driver_gives_install_hint(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = importlib.import_module

        def _missing(name: str, package: str | None = None) -> Any:
            if name.startswith("databricks"):
                raise ImportError(name)
            return real_import(name, package)

        monkeypatch.setattr(importlib, "import_module", _missing)
        health = await DatabricksConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "pip install databricks-sql-connector" in (health.message or "")


class TestReadOnlyEnforcement:
    async def test_warns_that_only_the_parse_guard_stands(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        connector = DatabricksConnector(_connection())
        health = await connector.test_connection()

        assert connector.read_only_enforcement() is ReadOnlyEnforcement.PARSE_ONLY
        assert health.status == "ok"
        assert len(health.warnings) == 1
        assert "parse guard" in health.warnings[0]


class TestConnect:
    async def test_connect_arguments(self, token: None, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch)
        connector = DatabricksConnector(
            _connection(params={"schema": "shop", "statement_timeout_ms": 45_500})
        )
        health = await connector.test_connection()

        assert health.status == "ok"
        kwargs = driver.connect_kwargs[0]
        assert kwargs["server_hostname"] == "dbc-1234.cloud.databricks.com"
        assert kwargs["http_path"] == "/sql/1.0/warehouses/abc123"
        assert kwargs["access_token"] == "dapi-secret"
        assert kwargs["catalog"] == "analytics"
        assert kwargs["schema"] == "shop"
        assert kwargs["session_configuration"] == {"STATEMENT_TIMEOUT": "46"}
        assert driver.connections[0].closed

    async def test_connect_failure_is_reported_not_raised(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            def connect(self, **_: Any) -> None:
                raise RuntimeError("invalid access token")

        monkeypatch.setattr(dbx_module, "_require_driver", lambda: _Boom())
        health = await DatabricksConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "invalid access token" in (health.message or "")


class TestIntrospection:
    async def test_relations_columns_keys(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        schemas = await DatabricksConnector(_connection()).introspect_schema()
        by_rel = {s.relation: s for s in schemas}

        assert set(by_rel) == {
            "shop.orders",
            "shop.customers",
            "shop.v_orders",
            "shop.mv_daily",
            "raw.events",
        }
        orders = by_rel["shop.orders"]
        assert orders.connection == "wh"
        assert orders.kind == "table"
        assert orders.acquisition_tier == AcquisitionTier.LIVE
        assert orders.row_count_estimate is None
        assert orders.primary_key == ["id"]
        assert [(c.name, c.type) for c in orders.columns] == [
            ("id", "int"),
            ("customer_id", "int"),
            ("amount", "decimal"),
            ("created_at", "timestamp"),
        ]
        assert [c.nullable for c in orders.columns] == [False, True, True, True]
        assert len(orders.foreign_keys) == 1
        fk = orders.foreign_keys[0]
        assert fk.columns == ["customer_id"]
        assert fk.references.relation == "shop.customers"
        assert fk.references.columns == ["id"]
        assert by_rel["shop.v_orders"].kind == "view"
        assert by_rel["shop.mv_daily"].kind == "materialized_view"
        assert by_rel["raw.events"].kind == "table"
        assert by_rel["raw.events"].columns[0].type == "json"

    async def test_foreign_key_without_a_reference_row_is_dropped(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = dict(_INTROSPECTION_RESULTS)
        results["referential_constraints"] = (
            ["CONSTRAINT_SCHEMA", "CONSTRAINT_NAME", "UNIQUE_CONSTRAINT_SCHEMA", "X"],
            [],
        )
        _install(monkeypatch, results)
        schemas = await DatabricksConnector(_connection()).introspect_schema()
        orders = next(s for s in schemas if s.relation == "shop.orders")

        assert orders.primary_key == ["id"]
        assert orders.foreign_keys == []

    async def test_composite_foreign_key_is_matched_by_position(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results = dict(_INTROSPECTION_RESULTS)
        names = _INTROSPECTION_RESULTS["key_column_usage"][0]
        results["key_column_usage"] = (
            names,
            [
                ("shop", "pk_customers", "PRIMARY KEY", "shop", "customers", "region", 1),
                ("shop", "pk_customers", "PRIMARY KEY", "shop", "customers", "id", 2),
                ("shop", "fk_orders_customer", "FOREIGN KEY", "shop", "orders", "region", 1),
                ("shop", "fk_orders_customer", "FOREIGN KEY", "shop", "orders", "customer_id", 2),
            ],
        )
        _install(monkeypatch, results)
        schemas = await DatabricksConnector(_connection()).introspect_schema()
        orders = next(s for s in schemas if s.relation == "shop.orders")

        fk = orders.foreign_keys[0]
        assert fk.columns == ["region", "customer_id"]
        assert fk.references.columns == ["region", "id"]

    async def test_key_query_failure_degrades_to_no_keys(
        self,
        token: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _install(monkeypatch, fail_on=("key_column_usage", "referential_constraints"))
        schemas = await DatabricksConnector(_connection()).introspect_schema()
        orders = next(s for s in schemas if s.relation == "shop.orders")

        assert [c.name for c in orders.columns] == ["id", "customer_id", "amount", "created_at"]
        assert orders.primary_key == []
        assert orders.foreign_keys == []
        assert "could not read Databricks key constraints" in caplog.text
        assert "could not read Databricks foreign key references" in caplog.text

    async def test_non_driver_error_in_key_query_still_propagates(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Cursor(FakeCursor):
            def execute(self, sql: str) -> None:
                if "key_column_usage" in sql:
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
            await DatabricksConnector(_connection()).introspect_schema()

    async def test_schema_and_table_filters(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        connector = DatabricksConnector(_connection(params={"schemas": ["shop"], "tables": ["o*"]}))
        relations = {s.relation for s in await connector.introspect_schema()}
        assert relations == {"shop.orders"}

    async def test_introspection_requires_catalog(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        conn = _connection()
        del conn.params["catalog"]
        with pytest.raises(ConnectionError, match="params.catalog"):
            await DatabricksConnector(conn).introspect_schema()

    async def test_catalog_name_is_quoted(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch)
        connector = DatabricksConnector(_connection(params={"catalog": "evil`; DROP"}))
        await connector.introspect_schema()
        executed = [sql for cur in driver.connections[0].cursors for sql in cur.executed]
        assert any("`evil``; DROP`.information_schema.tables" in sql for sql in executed)


class TestRunReadOnlySql:
    async def test_returns_rows_and_types(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(
            monkeypatch,
            {"SELECT": (["n", "total"], [(1, Decimal("2.50")), (2, Decimal("3.00"))])},
        )
        result = await DatabricksConnector(_connection()).run_read_only_sql("SELECT 1")
        assert [(c.name, c.type) for c in result.columns] == [("n", "int"), ("total", "decimal")]
        assert result.rows == [[1, Decimal("2.50")], [2, Decimal("3.00")]]
        assert not result.truncated

    async def test_truncates_at_row_limit(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, {"SELECT": (["n"], [(i,) for i in range(5)])})
        connector = DatabricksConnector(_connection(params={"row_limit": 3}))
        result = await connector.run_read_only_sql("SELECT n FROM t")
        assert result.truncated
        assert len(result.rows) == 3

    @pytest.mark.parametrize("sql", ["DELETE FROM t", "SELECT 1; SELECT 2", "DROP TABLE t"])
    async def test_rejects_writes_before_connecting(
        self, sql: str, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch)
        with pytest.raises(ReadOnlyViolation):
            await DatabricksConnector(_connection()).run_read_only_sql(sql)
        assert driver.connect_kwargs == []

    async def test_accepts_databricks_only_syntax(
        self, token: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, {"payload:user": (["uid"], [("u1",)])})
        sql = "SELECT payload:user.id AS uid FROM `analytics`.`raw`.`events`"
        result = await DatabricksConnector(_connection()).run_read_only_sql(sql)
        assert result.rows == [["u1"]]


class TestDescribeRelation:
    async def test_describe_table(self, token: None, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            {
                "DESCRIBE TABLE": (
                    ["col_name", "data_type", "comment"],
                    [
                        ("id", "bigint", None),
                        ("amount", "decimal(18,2)", None),
                        ("name", "string", None),
                        ("", "", ""),
                        ("# Partition Information", "", ""),
                        ("created_on", "date", None),
                    ],
                )
            },
        )
        cols = await DatabricksConnector(_connection()).describe_relation("shop.orders")
        assert [(c.name, c.type, c.position) for c in cols] == [
            ("id", "int", 1),
            ("amount", "decimal", 2),
            ("name", "string", 3),
        ]

    async def test_unsafe_relation_rejected(self, token: None) -> None:
        with pytest.raises(ValueError, match="unsafe relation"):
            await DatabricksConnector(_connection()).describe_relation("t; DROP TABLE x")


class TestNormalizeType:
    @pytest.mark.parametrize(
        ("raw", "scale", "expected"),
        [
            ("LONG", None, "int"),
            ("BIGINT", None, "int"),
            ("SHORT", None, "int"),
            ("TINYINT", None, "int"),
            ("DECIMAL", 0, "int"),
            ("DECIMAL", 2, "decimal"),
            ("decimal(18,0)", None, "int"),
            ("decimal(10,2)", None, "decimal"),
            ("DOUBLE", None, "float"),
            ("FLOAT", None, "float"),
            ("STRING", None, "string"),
            ("VARCHAR(100)", None, "string"),
            ("BOOLEAN", None, "bool"),
            ("DATE", None, "date"),
            ("TIMESTAMP", None, "timestamp"),
            ("TIMESTAMP_NTZ", None, "timestamp"),
            ("array<int>", None, "json"),
            ("MAP<STRING,INT>", None, "json"),
            ("STRUCT<a:INT>", None, "json"),
            ("VARIANT", None, "json"),
            ("SOMETHING_NEW", None, "json"),
        ],
    )
    def test_mapping(self, raw: str, scale: int | None, expected: str) -> None:
        assert _normalize_type(raw, "s.t", "c", scale) == expected
