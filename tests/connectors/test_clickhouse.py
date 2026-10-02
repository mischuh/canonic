"""ClickHouse connector unit tests, run against a fake clickhouse-connect so no server is needed."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

import canonic.connectors.clickhouse as clickhouse_module
from canonic.config import Connection
from canonic.connectors.base import Capability, ReadOnlyEnforcement
from canonic.connectors.clickhouse import ClickHouseConnector
from canonic.connectors.factory import default_factory
from canonic.exc import ConnectionError, CredentialError, ReadOnlyViolation

PASSWORD_ENV = "CANONIC_TEST_CLICKHOUSE_PASSWORD"


class FakeStream:
    """Stands in for the context manager ``query_row_block_stream`` returns."""

    def __init__(self, names: list[str], types: list[str], blocks: list[list[tuple[Any, ...]]]):
        self.source = SimpleNamespace(
            column_names=names, column_types=[SimpleNamespace(name=name) for name in types]
        )
        self._blocks = blocks

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(self._blocks)


class FakeClient:
    """Answers each statement with the first canned answer whose needle occurs in its SQL."""

    def __init__(self, answers: list[tuple[str, Any]], streams: list[FakeStream]) -> None:
        self.answers = answers
        self.streams = streams
        self.queries: list[tuple[str, Any, Any]] = []
        self.streamed: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> SimpleNamespace:
        self.queries.append((sql, parameters, settings))
        for needle, answer in self.answers:
            if needle in sql:
                if isinstance(answer, Exception):
                    raise answer
                return SimpleNamespace(result_rows=answer)
        return SimpleNamespace(result_rows=[])

    def query_row_block_stream(self, sql: str, settings: dict[str, Any]) -> FakeStream:
        self.streamed.append((sql, settings))
        return self.streams[0]

    def close(self) -> None:
        self.closed = True


class FakeDriver:
    """A stand-in for the ``clickhouse_connect`` module."""

    def __init__(
        self, answers: list[tuple[str, Any]] | None = None, stream: FakeStream | None = None
    ) -> None:
        self.answers = answers or []
        self.streams = [stream or FakeStream([], [], [])]
        self.connect_kwargs: list[dict[str, Any]] = []
        self.clients: list[FakeClient] = []
        self.connect_error: Exception | None = None

    def get_client(self, **kwargs: Any) -> FakeClient:
        if self.connect_error is not None:
            raise self.connect_error
        self.connect_kwargs.append(kwargs)
        client = FakeClient(self.answers, self.streams)
        self.clients.append(client)
        return client


def _connection(**params: Any) -> Connection:
    return Connection(
        id="events",
        type="clickhouse",
        params={"host": "ch.example.com", "user": "reader", "database": "events", **params},
        credentials_ref=f"env:{PASSWORD_ENV}",
    )


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PASSWORD_ENV, "s3cret")


def _install(monkeypatch: pytest.MonkeyPatch, driver: FakeDriver) -> FakeDriver:
    """Swap the driver import so the connector talks to ``driver``."""
    monkeypatch.setattr(clickhouse_module, "_require_driver", lambda: driver)
    return driver


class TestConstruction:
    def test_registered_in_default_factory(self) -> None:
        assert "clickhouse" in default_factory.registered_types()
        assert isinstance(default_factory.create(_connection()), ClickHouseConnector)

    def test_missing_credentials_ref_is_a_credential_error(self) -> None:
        connection = Connection(id="e", type="clickhouse", params={"host": "h", "user": "u"})
        with pytest.raises(CredentialError):
            ClickHouseConnector(connection)

    def test_construction_needs_no_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_driver(name: str) -> Any:
            raise AssertionError(f"driver {name} imported during construction")

        monkeypatch.setattr(clickhouse_module.importlib, "import_module", no_driver)
        ClickHouseConnector(_connection())

    def test_fetch_column_stats_is_ignored_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            ClickHouseConnector(_connection(fetch_column_stats=True))
        assert "fetch_column_stats" in caplog.text

    def test_capabilities(self) -> None:
        caps = ClickHouseConnector(_connection()).capabilities()
        assert Capability.RUN_READ_ONLY_SQL in caps
        assert Capability.INTROSPECT_SCHEMA in caps

    def test_reports_native_read_only_enforcement(self) -> None:
        connector = ClickHouseConnector(_connection())
        assert connector.read_only_enforcement() is ReadOnlyEnforcement.NATIVE
        assert connector.read_only_warnings() == ()


class TestConnect:
    async def test_connect_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver())
        await ClickHouseConnector(_connection(statement_timeout_ms=5000)).test_connection()

        [kwargs] = driver.connect_kwargs
        assert kwargs["host"] == "ch.example.com"
        assert kwargs["port"] == 8123
        assert kwargs["username"] == "reader"
        assert kwargs["password"] == "s3cret"
        assert kwargs["database"] == "events"
        assert kwargs["secure"] is False
        assert kwargs["send_receive_timeout"] == 5 + clickhouse_module._READ_TIMEOUT_GRACE_S

    async def test_secure_switches_the_default_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver())
        await ClickHouseConnector(_connection(secure=True)).test_connection()
        await ClickHouseConnector(_connection(secure=True, port=9440)).test_connection()

        assert [kwargs["port"] for kwargs in driver.connect_kwargs] == [8443, 9440]
        assert all(kwargs["secure"] is True for kwargs in driver.connect_kwargs)

    @pytest.mark.parametrize(
        ("value", "secure", "port"),
        [("true", True, 8443), ("True", True, 8443), ("false", False, 8123), (False, False, 8123)],
    )
    async def test_secure_accepts_the_strings_cli_params_produce(
        self, monkeypatch: pytest.MonkeyPatch, value: Any, secure: bool, port: int
    ) -> None:
        """``--param secure=false`` arrives as the string ``"false"``, which is truthy in Python."""
        driver = _install(monkeypatch, FakeDriver())
        await ClickHouseConnector(_connection(secure=value)).test_connection()

        assert driver.connect_kwargs[0]["secure"] is secure
        assert driver.connect_kwargs[0]["port"] == port

    async def test_defaults_for_user_and_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver())
        bare = Connection(
            id="b", type="clickhouse", params={"host": "h"}, credentials_ref=f"env:{PASSWORD_ENV}"
        )
        alias = Connection(
            id="a",
            type="clickhouse",
            params={"host": "h", "dbname": "other"},
            credentials_ref=f"env:{PASSWORD_ENV}",
        )
        await ClickHouseConnector(bare).test_connection()
        await ClickHouseConnector(alias).test_connection()

        assert driver.connect_kwargs[0]["username"] == "default"
        assert driver.connect_kwargs[0]["database"] == "default"
        assert driver.connect_kwargs[1]["database"] == "other"

    async def test_missing_driver_names_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = clickhouse_module.importlib.import_module

        def missing(name: str) -> Any:
            if name == "clickhouse_connect":
                raise ImportError(name)
            return real_import(name)

        monkeypatch.setattr(clickhouse_module.importlib, "import_module", missing)
        with pytest.raises(ConnectionError, match=r"canonic\[clickhouse\]"):
            await ClickHouseConnector(_connection()).run_read_only_sql("SELECT 1")

    async def test_client_is_closed_after_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver())
        await ClickHouseConnector(_connection()).test_connection()
        assert driver.clients[0].closed


class TestTestConnection:
    async def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakeDriver(answers=[("version()", [("26.9.1",)])]))
        health = await ClickHouseConnector(_connection()).test_connection()
        assert health.status == "ok"

    async def test_connect_failure_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver())
        driver.connect_error = OSError("connection refused")
        health = await ClickHouseConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "connection refused" in (health.message or "")

    async def test_query_failure_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeDriver(answers=[("version()", OSError("auth failed"))]))
        health = await ClickHouseConnector(_connection()).test_connection()
        assert health.status == "error"
        assert "auth failed" in (health.message or "")


class TestRunReadOnlySql:
    def _stream(self, blocks: list[list[tuple[Any, ...]]]) -> FakeStream:
        return FakeStream(
            ["id", "amount", "note"],
            ["UInt64", "Nullable(Decimal(12, 2))", "Array(String)"],
            blocks,
        )

    async def test_query_runs_with_readonly_two_and_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, FakeDriver(stream=self._stream([[(1, None, [])]])))
        await ClickHouseConnector(_connection(statement_timeout_ms=2500)).run_read_only_sql(
            "SELECT 1"
        )

        [(sql, settings)] = driver.clients[0].streamed
        assert sql == "SELECT 1"
        assert settings == {"readonly": 2, "max_execution_time": 3}

    async def test_result_columns_use_the_server_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeDriver(stream=self._stream([[(1, None, ["a"])]])))
        result = await ClickHouseConnector(_connection()).run_read_only_sql("SELECT 1")

        assert [(c.name, c.type) for c in result.columns] == [
            ("id", "int"),
            ("amount", "decimal"),
            ("note", "json"),
        ]
        assert result.rows == [[1, None, ["a"]]]
        assert result.truncated is False

    async def test_an_empty_result_still_has_its_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeDriver(stream=self._stream([])))
        result = await ClickHouseConnector(_connection()).run_read_only_sql("SELECT 1")
        assert [c.name for c in result.columns] == ["id", "amount", "note"]
        assert result.rows == []

    async def test_rows_beyond_the_limit_truncate_across_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocks = [[(1, None, []), (2, None, [])], [(3, None, []), (4, None, [])]]
        _install(monkeypatch, FakeDriver(stream=self._stream(blocks)))
        result = await ClickHouseConnector(_connection(row_limit=3)).run_read_only_sql("SELECT 1")

        assert [row[0] for row in result.rows] == [1, 2, 3]
        assert result.truncated is True

    async def test_exactly_the_limit_is_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeDriver(stream=self._stream([[(1, None, []), (2, None, [])]])))
        result = await ClickHouseConnector(_connection(row_limit=2)).run_read_only_sql("SELECT 1")
        assert len(result.rows) == 2
        assert result.truncated is False

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "DROP TABLE t",
            "ALTER TABLE t DELETE WHERE 1",
            "SELECT 1; SELECT 2",
        ],
    )
    async def test_writes_are_refused_before_any_connection_opens(
        self, monkeypatch: pytest.MonkeyPatch, sql: str
    ) -> None:
        driver = _install(monkeypatch, FakeDriver())
        with pytest.raises(ReadOnlyViolation):
            await ClickHouseConnector(_connection()).run_read_only_sql(sql)
        assert driver.clients == []

    async def test_clickhouse_only_syntax_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(monkeypatch, FakeDriver(stream=self._stream([])))
        sql = "SELECT quantileExactInclusive(0.5)(x) FROM t LEFT JOIN u ON t.i = u.i SETTINGS join_use_nulls = 1"
        await ClickHouseConnector(_connection()).run_read_only_sql(sql)
        assert driver.clients[0].streamed[0][0] == sql


_TABLES = [
    ("events", "page_views", "MergeTree", 1200),
    ("events", "v_daily", "View", None),
    ("events", "mv_hourly", "MaterializedView", None),
    ("other", "noise", "Memory", 3),
]

_COLUMNS = [
    ("events", "page_views", "id", "UInt64", 1),
    ("events", "page_views", "country", "LowCardinality(Nullable(String))", 2),
    ("events", "page_views", "revenue", "Decimal(18, 4)", 3),
    ("events", "page_views", "seen_at", "DateTime64(3, 'UTC')", 4),
    ("events", "page_views", "props", "Map(String, String)", 5),
    ("events", "v_daily", "day", "Date", 1),
    ("events", "mv_hourly", "hour", "DateTime", 1),
    ("other", "noise", "n", "Int8", 1),
    ("events", "empty_relation", "x", "Int8", 1),
]


class TestIntrospection:
    def _driver(self) -> FakeDriver:
        return FakeDriver(
            answers=[("FROM system.tables", _TABLES), ("FROM system.columns", _COLUMNS)]
        )

    async def test_relations_columns_and_kinds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, self._driver())
        schemas = {
            s.relation: s for s in await ClickHouseConnector(_connection()).introspect_schema()
        }

        assert set(schemas) == {
            "events.page_views",
            "events.v_daily",
            "events.mv_hourly",
            "other.noise",
        }
        assert schemas["events.page_views"].kind == "table"
        assert schemas["events.v_daily"].kind == "view"
        assert schemas["events.mv_hourly"].kind == "view"
        assert schemas["events.page_views"].row_count_estimate == 1200
        assert schemas["events.v_daily"].row_count_estimate is None

    async def test_column_types_and_nullability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, self._driver())
        schemas = {
            s.relation: s for s in await ClickHouseConnector(_connection()).introspect_schema()
        }

        columns = {c.name: c for c in schemas["events.page_views"].columns}
        assert (columns["id"].type, columns["id"].nullable) == ("int", False)
        assert (columns["country"].type, columns["country"].nullable) == ("string", True)
        assert columns["revenue"].type == "decimal"
        assert columns["seen_at"].type == "timestamp"
        assert columns["props"].type == "json"
        assert [c.position for c in schemas["events.page_views"].columns] == [1, 2, 3, 4, 5]

    async def test_no_primary_key_or_foreign_keys_are_asserted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, self._driver())
        for schema in await ClickHouseConnector(_connection()).introspect_schema():
            assert schema.primary_key == []
            assert schema.foreign_keys == []

    async def test_schemas_and_tables_filters_narrow_the_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, self._driver())
        schemas = await ClickHouseConnector(_connection(schemas=["events"])).introspect_schema()
        assert {s.relation.split(".")[0] for s in schemas} == {"events"}

    async def test_system_databases_are_excluded_in_the_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(monkeypatch, self._driver())
        await ClickHouseConnector(_connection()).introspect_schema()
        for sql, _, _ in driver.clients[0].queries:
            assert "'system'" in sql
            assert "'INFORMATION_SCHEMA'" in sql

    async def test_unmapped_type_warns_and_becomes_json(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _install(monkeypatch, self._driver())
        with caplog.at_level(logging.WARNING):
            await ClickHouseConnector(_connection()).introspect_schema()
        assert "Map(String, String)" in caplog.text

    async def test_fingerprint_is_stable_between_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, self._driver())
        connector = ClickHouseConnector(_connection())
        first = await connector.introspect_schema()
        second = await connector.introspect_schema()
        assert [s.source_fingerprint for s in first] == [s.source_fingerprint for s in second]


class TestDescribeRelation:
    async def test_describes_a_qualified_relation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        driver = _install(
            monkeypatch,
            FakeDriver(
                answers=[
                    (
                        "FROM system.columns",
                        [("id", "UInt64", 1), ("country", "Nullable(String)", 2)],
                    )
                ]
            ),
        )
        columns = await ClickHouseConnector(_connection()).describe_relation("events.page_views")

        assert [(c.name, c.type, c.nullable, c.position) for c in columns] == [
            ("id", "int", False, 1),
            ("country", "string", True, 2),
        ]
        [(_, parameters, _)] = driver.clients[0].queries
        assert parameters == {"db": "events", "tbl": "page_views"}

    async def test_bare_name_uses_the_default_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _install(
            monkeypatch, FakeDriver(answers=[("FROM system.columns", [("id", "UInt64", 1)])])
        )
        await ClickHouseConnector(_connection()).describe_relation("page_views")
        assert driver.clients[0].queries[0][1] == {"db": "", "tbl": "page_views"}

    async def test_missing_relation_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeDriver())
        with pytest.raises(ValueError, match="not found"):
            await ClickHouseConnector(_connection()).describe_relation("events.nope")

    @pytest.mark.parametrize("relation", ["a.b.c", "events.", ""])
    async def test_unsafe_identifiers_are_rejected(
        self, monkeypatch: pytest.MonkeyPatch, relation: str
    ) -> None:
        _install(monkeypatch, FakeDriver())
        with pytest.raises(ValueError, match="unsafe"):
            await ClickHouseConnector(_connection()).describe_relation(relation)


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("type_name", "expected"),
        [
            ("UInt8", "int"),
            ("Int256", "int"),
            ("Nullable(Int32)", "int"),
            ("LowCardinality(String)", "string"),
            ("LowCardinality(Nullable(String))", "string"),
            ("FixedString(16)", "string"),
            ("UUID", "string"),
            ("Enum8('a' = 1, 'b' = 2)", "string"),
            ("Float32", "float"),
            ("Decimal(38, 10)", "decimal"),
            ("Decimal128(5)", "decimal"),
            ("Bool", "bool"),
            ("Date32", "date"),
            ("DateTime", "timestamp"),
            ("DateTime64(3, 'Europe/Berlin')", "timestamp"),
            ("Array(Int32)", None),
            ("Tuple(a Int32, b String)", None),
        ],
    )
    def test_normalize_type(self, type_name: str, expected: str | None) -> None:
        assert clickhouse_module._normalize_type(type_name) == expected
