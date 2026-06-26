"""Tests for the DuckDB connector.

Unit tests cover read-only enforcement and type mapping with no I/O.
Integration tests exercise a real in-memory DuckDB seeded with a reference schema
(no Docker required — DuckDB is bundled as a Python extension).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import duckdb
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

from canon.config import Connection
from canon.connectors.base import AcquisitionTier, Capability
from canon.connectors.duckdb import DuckDBConnector, _normalize_type
from canon.exc import ReadOnlyViolation

_SEED_SQL = """
CREATE TABLE dim_customers (
    customer_id INTEGER PRIMARY KEY,
    name        VARCHAR NOT NULL,
    created_at  TIMESTAMP
);

CREATE TABLE fct_orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES dim_customers (customer_id),
    amount      DECIMAL(12, 2),
    order_date  DATE
);

CREATE VIEW v_order_summary AS
    SELECT o.order_id, c.name AS customer_name, o.amount
    FROM fct_orders o
    JOIN dim_customers c ON c.customer_id = o.customer_id;

INSERT INTO dim_customers VALUES (1, 'Alice', '2024-01-01');
INSERT INTO dim_customers VALUES (2, 'Bob',   '2024-02-01');
INSERT INTO fct_orders VALUES (101, 1, 99.99,  '2024-03-01');
INSERT INTO fct_orders VALUES (102, 2, 149.50, '2024-03-02');
"""


@pytest.fixture
def duckdb_db(tmp_path: Path) -> Iterator[tuple[DuckDBConnector, Path]]:
    """A seeded DuckDB file connector with analytic tables."""
    db_path = tmp_path / "analytics.duckdb"
    con = duckdb.connect(str(db_path))
    con.executemany("", [])  # ensure file is created
    con.execute(_SEED_SQL)
    con.close()

    connection = Connection(
        id="warehouse_duckdb",
        type="duckdb",
        params={"path": str(db_path), "row_limit": 5},
    )
    yield DuckDBConnector(connection), db_path


@pytest.fixture
def duckdb_offline_connector() -> DuckDBConnector:
    """A DuckDB connector pointed at :memory: (no I/O on construction)."""
    connection = Connection(
        id="warehouse_duckdb",
        type="duckdb",
        params={"path": ":memory:"},
    )
    return DuckDBConnector(connection)


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
            ("SMALLINT", "int"),
            ("TINYINT", "int"),
            ("HUGEINT", "int"),
            ("UBIGINT", "int"),
            ("FLOAT", "float"),
            ("REAL", "float"),
            ("DOUBLE", "float"),
            ("DOUBLE PRECISION", "float"),
            ("DECIMAL(12, 2)", "decimal"),
            ("NUMERIC", "decimal"),
            ("VARCHAR", "string"),
            ("TEXT", "string"),
            ("CHAR", "string"),
            ("UUID", "string"),
            ("ENUM", "string"),
            ("BOOLEAN", "bool"),
            ("BOOL", "bool"),
            ("DATE", "date"),
            ("TIMESTAMP", "timestamp"),
            ("TIMESTAMPTZ", "timestamp"),
            ("TIMESTAMP WITH TIME ZONE", "timestamp"),
            ("TIMESTAMP_S", "timestamp"),
            ("JSON", "json"),
            ("BLOB", "json"),
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
    def test_capabilities(self, duckdb_offline_connector: DuckDBConnector) -> None:
        assert set(duckdb_offline_connector.capabilities()) == {
            Capability.INTROSPECT_SCHEMA,
            Capability.RUN_READ_ONLY_SQL,
            Capability.TEST_CONNECTION,
            Capability.CAPABILITIES,
        }

    def test_no_query_history_capability(self, duckdb_offline_connector: DuckDBConnector) -> None:
        assert Capability.READ_QUERY_HISTORY not in duckdb_offline_connector.capabilities()

    def test_no_extract_definitions_capability(
        self, duckdb_offline_connector: DuckDBConnector
    ) -> None:
        assert Capability.EXTRACT_DEFINITIONS not in duckdb_offline_connector.capabilities()


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
async def test_read_only_enforcement(duckdb_offline_connector: DuckDBConnector, sql: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        await duckdb_offline_connector.run_read_only_sql(sql)


# ---------------------------------------------------------------------------
# Integration: real DuckDB file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_ok(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    health = await connector.test_connection()
    assert health.status == "ok"
    assert health.message is None


@pytest.mark.asyncio
async def test_connection_bad_path() -> None:
    connection = Connection(
        id="bad_duckdb",
        type="duckdb",
        params={"path": "/nonexistent/path/that/definitely/does/not/exist.duckdb"},
    )
    connector = DuckDBConnector(connection)
    health = await connector.test_connection()
    assert health.status == "error"
    assert health.message


@pytest.mark.asyncio
async def test_introspect_schema_tables(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}

    assert "main.dim_customers" in schemas
    assert "main.fct_orders" in schemas
    assert "main.v_order_summary" in schemas


@pytest.mark.asyncio
async def test_introspect_schema_table_kind(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    assert schemas["main.dim_customers"].kind == "table"
    assert schemas["main.v_order_summary"].kind == "view"


@pytest.mark.asyncio
async def test_introspect_schema_columns(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    orders = schemas["main.fct_orders"]

    col_types = {c.name: c.type for c in orders.columns}
    assert col_types["order_id"] == "int"
    assert col_types["customer_id"] == "int"
    assert col_types["amount"] == "decimal"
    assert col_types["order_date"] == "date"


@pytest.mark.asyncio
async def test_introspect_schema_primary_key(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    assert schemas["main.fct_orders"].primary_key == ["order_id"]
    assert schemas["main.dim_customers"].primary_key == ["customer_id"]


@pytest.mark.asyncio
async def test_introspect_schema_foreign_key(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    schemas = {s.relation: s for s in await connector.introspect_schema()}
    orders = schemas["main.fct_orders"]
    assert any(
        fk.references.relation == "main.dim_customers" and fk.columns == ["customer_id"]
        for fk in orders.foreign_keys
    )


@pytest.mark.asyncio
async def test_introspect_schema_fingerprint(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    schemas = await connector.introspect_schema()
    for schema in schemas:
        assert schema.source_fingerprint is not None
        assert schema.source_fingerprint.startswith("sha256:")
        assert schema.acquisition_tier == AcquisitionTier.LIVE
        assert schema.connection == "warehouse_duckdb"


@pytest.mark.asyncio
async def test_run_read_only_sql_returns_typed_resultset(
    duckdb_db: tuple[DuckDBConnector, Path],
) -> None:
    connector, _ = duckdb_db
    result = await connector.run_read_only_sql("SELECT 1 AS a, 'hello' AS b")
    assert [c.name for c in result.columns] == ["a", "b"]
    assert result.rows == [[1, "hello"]]
    assert result.truncated is False


@pytest.mark.asyncio
async def test_run_read_only_sql_queries_seeded_data(
    duckdb_db: tuple[DuckDBConnector, Path],
) -> None:
    connector, _ = duckdb_db
    result = await connector.run_read_only_sql(
        "SELECT order_id, amount FROM fct_orders ORDER BY order_id"
    )
    assert len(result.rows) == 2
    assert result.rows[0][0] == 101


@pytest.mark.asyncio
async def test_row_limit_enforced(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    # duckdb_db fixture sets row_limit=5; generate more rows with a range query
    connector, db_path = duckdb_db
    con = duckdb.connect(str(db_path))
    con.execute(
        "INSERT INTO dim_customers SELECT i, 'name' || i, '2024-01-01' FROM range(3, 10) t(i)"
    )
    con.close()

    result = await connector.run_read_only_sql("SELECT customer_id FROM dim_customers")
    assert len(result.rows) == 5
    assert result.truncated is True


@pytest.mark.asyncio
async def test_describe_relation(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    cols = await connector.describe_relation("main.dim_customers")
    col_names = [c.name for c in cols]
    assert "customer_id" in col_names
    assert "name" in col_names


@pytest.mark.asyncio
async def test_describe_relation_bare_name(duckdb_db: tuple[DuckDBConnector, Path]) -> None:
    connector, _ = duckdb_db
    cols = await connector.describe_relation("fct_orders")
    assert any(c.name == "order_id" for c in cols)


@pytest.mark.asyncio
async def test_describe_relation_rejects_unsafe_name(
    duckdb_db: tuple[DuckDBConnector, Path],
) -> None:
    connector, _ = duckdb_db
    with pytest.raises(ValueError, match="unsafe relation"):
        await connector.describe_relation("main.table; DROP TABLE dim_customers")
