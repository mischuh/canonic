"""Tests for the SQLite connector.

Unit tests cover read-only enforcement, type mapping, and the capability
surface with no I/O.  Integration tests exercise a real SQLite file seeded
with a reference schema (no Docker required — SQLite is built into Python).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
import pytest

from canon.config import Connection
from canon.connectors.base import AcquisitionTier, Capability
from canon.connectors.sqlite import SQLiteConnector, _normalize_type
from canon.exc import ReadOnlyViolation

# ---------------------------------------------------------------------------
# Seed SQL mirroring the Postgres fixture schema to enable cross-connector
# comparison tests (shared analytics schema with FK, composite PK, etc.)
# ---------------------------------------------------------------------------

_SEED_SQL = """
CREATE TABLE dim_customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT
);

CREATE TABLE fct_orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES dim_customers (customer_id),
    amount      NUMERIC(12, 2),
    metadata    TEXT,
    order_date  TEXT
);

CREATE VIEW v_order_summary AS
    SELECT o.order_id, c.name AS customer_name, o.amount
    FROM fct_orders o
    JOIN dim_customers c ON c.customer_id = o.customer_id;

INSERT INTO dim_customers VALUES (1, 'Alice', '2024-01-01');
INSERT INTO dim_customers VALUES (2, 'Bob',   '2024-02-01');
INSERT INTO fct_orders VALUES (101, 1, 99.99,  NULL, '2024-03-01');
INSERT INTO fct_orders VALUES (102, 2, 149.50, NULL, '2024-03-02');
"""


@pytest.fixture
async def sqlite_db(tmp_path: Path) -> AsyncIterator[tuple[SQLiteConnector, Path]]:
    """A seeded SQLite file connector with analytic tables."""
    db_path = tmp_path / "analytics.db"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(_SEED_SQL)
        await db.commit()

    connection = Connection(
        id="warehouse_sqlite",
        type="sqlite",
        params={"path": str(db_path), "row_limit": 5},
    )
    connector = SQLiteConnector(connection)
    yield connector, db_path


# ---------------------------------------------------------------------------
# Unit: type mapping
# ---------------------------------------------------------------------------


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("INTEGER", "int"),
            ("INT", "int"),
            ("BIGINT", "int"),
            ("TINYINT", "int"),
            ("SMALLINT", "int"),
            ("REAL", "float"),
            ("FLOAT", "float"),
            ("DOUBLE", "float"),
            ("DOUBLE PRECISION", "float"),
            ("NUMERIC(12, 2)", "decimal"),
            ("DECIMAL", "decimal"),
            ("NUMBER", "decimal"),
            ("TEXT", "string"),
            ("VARCHAR(50)", "string"),
            ("CHARACTER(10)", "string"),
            ("NVARCHAR(100)", "string"),
            ("CLOB", "string"),
            ("BLOB", "json"),
            ("JSON", "json"),
            ("BOOLEAN", "bool"),
            ("BOOL", "bool"),
            ("DATE", "date"),
            ("DATETIME", "timestamp"),
            ("TIMESTAMP", "timestamp"),
            ("TIME", "timestamp"),
            ("UUID", "string"),
        ],
    )
    def test_known_types(self, raw: str, expected: str) -> None:
        assert _normalize_type(raw, "main.t", "c") == expected

    def test_empty_type_maps_to_json(self) -> None:
        assert _normalize_type("", "main.t", "c") == "json"

    def test_unmapped_type_warns_and_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            result = _normalize_type("GEOGRAPHY", "main.t", "geom")
        assert result == "json"
        assert "GEOGRAPHY" in caplog.text


# ---------------------------------------------------------------------------
# Unit: capabilities and surface
# ---------------------------------------------------------------------------


class TestConnectorSurface:
    def test_capabilities(self, sqlite_offline_connector: SQLiteConnector) -> None:
        assert set(sqlite_offline_connector.capabilities()) == {
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        }

    def test_no_query_history_capability(self, sqlite_offline_connector: SQLiteConnector) -> None:
        assert Capability.READ_QUERY_HISTORY not in sqlite_offline_connector.capabilities()


# ---------------------------------------------------------------------------
# Unit: read-only enforcement (no I/O — guard fires before connect)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "SELECT 1; SELECT 2",
    ],
)
@pytest.mark.asyncio
async def test_read_only_enforcement(sqlite_offline_connector: SQLiteConnector, sql: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        await sqlite_offline_connector.run_read_only_sql(sql)


# ---------------------------------------------------------------------------
# Integration: real SQLite file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_ok(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    health = await connector.test_connection()
    assert health.status == "ok"
    assert health.message is None


@pytest.mark.asyncio
async def test_connection_bad_path() -> None:
    connection = Connection(
        id="bad_sqlite",
        type="sqlite",
        params={"path": "/nonexistent/path/that/definitely/does/not/exist.db"},
    )
    connector = SQLiteConnector(connection)
    health = await connector.test_connection()
    assert health.status == "error"
    assert health.message


@pytest.mark.asyncio
async def test_introspect_schema_tables(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}

    assert "main.dim_customers" in schemas
    assert "main.fct_orders" in schemas
    assert "main.v_order_summary" in schemas


@pytest.mark.asyncio
async def test_introspect_schema_table_kind(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    assert schemas["main.dim_customers"].kind == "table"
    assert schemas["main.v_order_summary"].kind == "view"


@pytest.mark.asyncio
async def test_introspect_schema_columns(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    orders = schemas["main.fct_orders"]

    col_types = {c.name: c.type for c in orders.columns}
    assert col_types["order_id"] == "int"
    assert col_types["customer_id"] == "int"
    assert col_types["amount"] == "decimal"


@pytest.mark.asyncio
async def test_introspect_schema_primary_key(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    assert schemas["main.fct_orders"].primary_key == ["order_id"]
    assert schemas["main.dim_customers"].primary_key == ["customer_id"]


@pytest.mark.asyncio
async def test_introspect_schema_foreign_key(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    orders = schemas["main.fct_orders"]
    assert any(
        fk.references.relation == "main.dim_customers" and fk.columns == ["customer_id"]
        for fk in orders.foreign_keys
    )


@pytest.mark.asyncio
async def test_introspect_schema_fingerprint(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    schemas = await connector.introspect_schema()
    for schema in schemas:
        assert schema.source_fingerprint is not None
        assert schema.source_fingerprint.startswith("sha256:")
        assert schema.acquisition_tier == AcquisitionTier.LIVE
        assert schema.connection == "warehouse_sqlite"


@pytest.mark.asyncio
async def test_fetch_column_stats_logs_warning_and_omits_stats(
    sqlite_db: tuple[SQLiteConnector, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """SQLite has no queryable planner statistics — the option degrades gracefully."""
    _, db_path = sqlite_db
    connection = Connection(
        id="warehouse_sqlite",
        type="sqlite",
        params={"path": str(db_path), "fetch_column_stats": True},
    )
    connector = SQLiteConnector(connection)

    with caplog.at_level(logging.WARNING):
        schemas = await connector.introspect_schema()

    assert "fetch_column_stats" in caplog.text
    orders = next(s for s in schemas if s.relation == "main.fct_orders")
    for col in orders.columns:
        assert col.stats_source is None
        assert col.distinct_count_estimate is None
        assert col.null_fraction is None


@pytest.mark.asyncio
async def test_run_read_only_sql_returns_typed_resultset(
    sqlite_db: tuple[SQLiteConnector, Path],
) -> None:
    connector, _ = sqlite_db
    result = await connector.run_read_only_sql("SELECT 1 AS a, 'hello' AS b")
    assert [c.name for c in result.columns] == ["a", "b"]
    assert result.rows == [[1, "hello"]]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_run_read_only_sql_queries_seeded_data(
    sqlite_db: tuple[SQLiteConnector, Path],
) -> None:
    connector, _ = sqlite_db
    result = await connector.run_read_only_sql(
        "SELECT order_id, amount FROM fct_orders ORDER BY order_id"
    )
    assert len(result.rows) == 2
    assert result.rows[0][0] == 101


@pytest.mark.asyncio
async def test_row_limit_enforced(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    # sqlite_db fixture sets row_limit=5; generate more rows via dim_customers self-join
    connector, db_path = sqlite_db
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(
            "INSERT INTO dim_customers VALUES (3,'C','2024-01-01');\n"
            "INSERT INTO dim_customers VALUES (4,'D','2024-01-01');\n"
            "INSERT INTO dim_customers VALUES (5,'E','2024-01-01');\n"
            "INSERT INTO dim_customers VALUES (6,'F','2024-01-01');\n"
            "INSERT INTO dim_customers VALUES (7,'G','2024-01-01');\n"
        )
        await db.commit()

    result = await connector.run_read_only_sql("SELECT customer_id FROM dim_customers")
    assert len(result.rows) == 5
    assert result.truncated is True


@pytest.mark.asyncio
async def test_describe_relation(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    cols = await connector.describe_relation("main.dim_customers")
    col_names = [c.name for c in cols]
    assert "customer_id" in col_names
    assert "name" in col_names


@pytest.mark.asyncio
async def test_describe_relation_bare_name(sqlite_db: tuple[SQLiteConnector, Path]) -> None:
    connector, _ = sqlite_db
    cols = await connector.describe_relation("fct_orders")
    assert any(c.name == "order_id" for c in cols)


@pytest.mark.asyncio
async def test_describe_relation_rejects_unsafe_name(
    sqlite_db: tuple[SQLiteConnector, Path],
) -> None:
    connector, _ = sqlite_db
    with pytest.raises(ValueError, match="unsafe relation"):
        await connector.describe_relation("main.table; DROP TABLE dim_customers")
