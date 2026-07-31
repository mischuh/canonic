"""Fixtures for the walking-skeleton e2e tests (GH-14).

A live PostgreSQL 16 is provided via testcontainers (the project convention; see
``tests/connectors/conftest.py``) and seeded with the ecommerce fixture data. The
fixture project under ``fixture_project/`` is copied to a tmp dir and its
``canonic.yaml`` rewritten with the container's coordinates so both serving surfaces
(CLI and MCP) talk to the same database.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from ruamel.yaml import YAML

from canonic.core.service import CanonicService

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TESTCONTAINERS = True
except ImportError:  # pragma: no cover - exercised only without the optional dep
    _HAS_TESTCONTAINERS = False

_FIXTURE_PROJECT = Path(__file__).parent / "fixture_project"

# The e2e fixture project's semantic sources (customers, orders — see
# fixture_project/semantics/warehouse_pg/) only declare the columns/tables they need,
# so seeding with the *actual* shipped example SQL is safe even though it also creates
# dim_products/dim_channels/fct_orders_rt/fct_order_items and an extra fct_orders.channel_id
# column: those are simply unreferenced by this fixture's semantics. Executing the real
# file (rather than a hand-copied subset) means this test can never silently drift from
# the example a user actually runs (GH-14 follow-up).
_ECOMMERCE_SETUP_SQL = (
    Path(__file__).parents[2] / "examples" / "ecommerce" / "setup.sql"
).read_text()

# Revenue after the revenue-excludes-refunds guardrail (status != 'refunded'), per
# examples/ecommerce/setup.sql: 500 + 350 + 125.50 + 780 + 430 + 1200 + 310 + 95 = 3790.50
EXPECTED_REVENUE = "3790.50"


async def _seed(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_ECOMMERCE_SETUP_SQL)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def e2e_postgres() -> Iterator[dict[str, Any]]:
    """Start and seed a Postgres 16 container; skip cleanly without Docker."""
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
            "host": host,
            "port": port,
            "user": container.username,
            "dbname": container.dbname,
            "password": container.password,
        }
    finally:
        container.stop()


@pytest.fixture
def e2e_project(
    e2e_postgres: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Copy the fixture project to a tmp dir, wired to the live container.

    Rewrites the connection params (host/port/user/dbname) — which are static in
    canonic.yaml — and exports the password via ``CANONIC_PG_PASSWORD``.
    """
    root = tmp_path / "project"
    shutil.copytree(_FIXTURE_PROJECT, root)

    config_path = root / "canonic.yaml"
    yaml = YAML()
    data = yaml.load(config_path.read_text())
    params = data["connections"][0]["params"]
    params["host"] = e2e_postgres["host"]
    params["port"] = e2e_postgres["port"]
    params["user"] = e2e_postgres["user"]
    params["dbname"] = e2e_postgres["dbname"]
    with config_path.open("w") as f:
        yaml.dump(data, f)

    monkeypatch.setenv("CANONIC_PG_PASSWORD", e2e_postgres["password"])
    return root


@pytest.fixture
def e2e_service(e2e_project: Path) -> CanonicService:
    """A CanonicService loaded from the live-wired fixture project."""
    return CanonicService.from_project(e2e_project)
