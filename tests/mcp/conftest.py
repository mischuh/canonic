"""Fixtures for MCP adapter tests. Shared fixtures live in tests/conftest.py."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TESTCONTAINERS = True
except ImportError:  # pragma: no cover - exercised only without the optional dep
    _HAS_TESTCONTAINERS = False

#: Credentials ``canonic_service`` expects: it exports ``PG_PASSWORD=testpw`` for ``env:PG_PASSWORD``.
_USER = "test"
_PASSWORD = "testpw"
_DBNAME = "testdb"

_ORDERS_SQL = """
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE analytics.fct_orders (
    order_id   text PRIMARY KEY,
    amount     numeric(12, 2) NOT NULL,
    status     text NOT NULL,
    region     text NOT NULL,
    channel    text NOT NULL,
    created_at timestamp NOT NULL
);

INSERT INTO analytics.fct_orders VALUES
    ('o1', 100.00, 'completed', 'emea', 'web', '2026-01-10 10:00:00'),
    ('o2', 250.50, 'completed', 'amer', 'app', '2026-01-11 11:00:00'),
    ('o3', 80.00, 'refunded', 'emea', 'web', '2026-01-12 12:00:00');
"""


async def _seed(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_ORDERS_SQL)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def orders_postgres() -> Iterator[dict[str, Any]]:
    """Connection params of a PostgreSQL container holding ``analytics.fct_orders``.

    The test is skipped when Docker is not available, like the connector fixtures.
    """
    if not _HAS_TESTCONTAINERS:
        pytest.skip("testcontainers not installed")
    try:
        container = PostgresContainer(
            "postgres:16-alpine", username=_USER, password=_PASSWORD, dbname=_DBNAME
        )
        container.start()
    except Exception as exc:  # Docker not running / image unavailable
        pytest.skip(f"Docker unavailable: {exc}")
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        asyncio.run(_seed(f"postgresql://{_USER}:{_PASSWORD}@{host}:{port}/{_DBNAME}"))
        yield {"host": host, "port": port, "dbname": _DBNAME, "user": _USER}
    finally:
        container.stop()
