"""Fixtures for connector tests.

Unit fixtures (``offline_connector``, ``dbt_manifest_path``) need no database. Integration
fixtures (``postgres_container``, ``pg_connector``) spin up a real PostgreSQL via
testcontainers and are skipped cleanly when Docker/testcontainers is absent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

from canon.config import Connection
from canon.connectors.postgres import PostgresConnector

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TESTCONTAINERS = True
except ImportError:  # pragma: no cover - exercised only without the optional dep
    _HAS_TESTCONTAINERS = False


_SEED_SQL = """
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE analytics.dim_customers (
    customer_id integer PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz
);

CREATE TABLE analytics.fct_orders (
    order_id    bigint PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES analytics.dim_customers (customer_id),
    amount      numeric(12, 2),
    metadata    jsonb,
    order_date  date
);
"""


async def _seed(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_SEED_SQL)
    finally:
        await conn.close()


@pytest.fixture
def dbt_manifest_path() -> Path:
    """Path to the compiled dbt manifest fixture used by dbt connector tests."""
    return Path(__file__).parent / "fixtures" / "dbt_manifest.json"


@pytest.fixture
def notion_pages_path() -> Path:
    """Path to the Notion pages fixture used by Notion connector tests."""
    return Path(__file__).parent / "fixtures" / "notion_pages.json"


@pytest.fixture
def metabase_questions_path() -> Path:
    """Path to the Metabase questions fixture used by Metabase connector tests."""
    return Path(__file__).parent / "fixtures" / "metabase_questions.json"


@pytest.fixture
def looker_looks_path() -> Path:
    """Path to the Looker looks fixture used by Looker connector tests."""
    return Path(__file__).parent / "fixtures" / "looker_looks.json"


@pytest.fixture
def offline_connector(monkeypatch: pytest.MonkeyPatch) -> PostgresConnector:
    """A connector that resolves credentials but never connects (unit tests)."""
    monkeypatch.setenv("CANON_TEST_PG_PASSWORD", "secret")
    connection = Connection(
        id="warehouse_pg",
        type="postgres",
        params={"host": "localhost", "port": 5432, "user": "u", "dbname": "db"},
        credentials_ref="env:CANON_TEST_PG_PASSWORD",
    )
    return PostgresConnector(connection)


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[dict[str, Any]]:
    if not _HAS_TESTCONTAINERS:
        pytest.skip("testcontainers not installed")
    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # Docker not running / image unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        dsn = (
            f"postgresql://{container.username}:{container.password}"
            f"@{host}:{port}/{container.dbname}"
        )
        asyncio.run(_seed(dsn))
        yield {
            "params": {
                "host": host,
                "port": port,
                "user": container.username,
                "dbname": container.dbname,
            },
            "password": container.password,
        }
    finally:
        container.stop()


@pytest.fixture
async def pg_connector(
    postgres_container: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[PostgresConnector]:
    monkeypatch.setenv("CANON_TEST_PG_PASSWORD", postgres_container["password"])
    connection = Connection(
        id="warehouse_pg",
        type="postgres",
        params={**postgres_container["params"], "row_limit": 5, "statement_timeout_ms": 5000},
        credentials_ref="env:CANON_TEST_PG_PASSWORD",
    )
    connector = PostgresConnector(connection)
    try:
        yield connector
    finally:
        await connector.aclose()
