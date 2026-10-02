"""Live tests for the Databricks connector against a real workspace (``integration``).

Skipped unless ``CANONIC_TEST_DATABRICKS_HOST`` is set. One-time setup: run
``fixtures/databricks_live_setup.sql`` in a SQL editor. It creates the ``canonic_test.shop``
data these tests assert on and grants a service principal read access only.

Environment:
    CANONIC_TEST_DATABRICKS_HOST         server hostname, required
    CANONIC_TEST_DATABRICKS_HTTP_PATH    SQL warehouse HTTP path, required
    CANONIC_TEST_DATABRICKS_TOKEN        access token of the read-only principal, required
    CANONIC_TEST_DATABRICKS_CATALOG      default canonic_test
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from canonic.config import Connection
from canonic.connectors.databricks import DatabricksConnector
from canonic.exc import ReadOnlyViolation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CANONIC_TEST_DATABRICKS_HOST"),
        reason="set CANONIC_TEST_DATABRICKS_{HOST,HTTP_PATH,TOKEN} to run",
    ),
]

_PREFIX = "CANONIC_TEST_DATABRICKS_"
_CATALOG = os.environ.get(f"{_PREFIX}CATALOG", "canonic_test")
_ORDERS = f"`{_CATALOG}`.`shop`.`orders`"


def _connector(**extra_params: Any) -> DatabricksConnector:
    params: dict[str, Any] = {
        "server_hostname": os.environ[f"{_PREFIX}HOST"],
        "http_path": os.environ[f"{_PREFIX}HTTP_PATH"],
        "catalog": _CATALOG,
        "schemas": ["shop"],
        **extra_params,
    }
    return DatabricksConnector(
        Connection(
            id="live",
            type="databricks",
            params=params,
            credentials_ref=f"env:{_PREFIX}TOKEN",
        )
    )


@pytest.fixture
def connector() -> DatabricksConnector:
    return _connector()


async def test_connection_reports_the_parse_only_warning(connector: DatabricksConnector) -> None:
    health = await connector.test_connection()
    assert health.status == "ok"
    assert len(health.warnings) == 1


async def test_introspection_relations_and_kinds(connector: DatabricksConnector) -> None:
    by_relation = {s.relation: s for s in await connector.introspect_schema()}

    assert {"shop.orders", "shop.customers", "shop.v_orders"} <= set(by_relation)
    assert by_relation["shop.orders"].kind == "table"
    assert by_relation["shop.v_orders"].kind == "view"


async def test_introspection_columns_and_declared_keys(connector: DatabricksConnector) -> None:
    orders = {s.relation: s for s in await connector.introspect_schema()}["shop.orders"]

    assert [(c.name, c.type) for c in orders.columns] == [
        ("id", "int"),
        ("customer_id", "int"),
        ("amount", "decimal"),
        ("created_at", "timestamp"),
    ]
    assert orders.primary_key == ["id"]
    assert [
        (f.columns, f.references.relation, f.references.columns) for f in orders.foreign_keys
    ] == [(["customer_id"], "shop.customers", ["id"])]


async def test_describe_view(connector: DatabricksConnector) -> None:
    columns = await connector.describe_relation(f"{_CATALOG}.shop.v_orders")
    assert [c.name for c in columns] == ["id", "customer_id", "amount"]


async def test_exact_percentile_and_date_trunc(connector: DatabricksConnector) -> None:
    percentile = await connector.run_read_only_sql(
        f"SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY `amount`) AS p FROM {_ORDERS}"
    )
    assert float(percentile.rows[0][0]) == 20.0

    monthly = await connector.run_read_only_sql(
        f"SELECT DATE_TRUNC('MONTH', `created_at`) AS m, COUNT(*) AS n FROM {_ORDERS} "
        "WHERE `created_at` >= CURRENT_DATE - INTERVAL '3650' DAY GROUP BY 1 ORDER BY 1"
    )
    assert [row[1] for row in monthly.rows] == [1, 2]


async def test_row_limit_truncates() -> None:
    result = await _connector(row_limit=2).run_read_only_sql(f"SELECT `id` FROM {_ORDERS}")
    assert result.truncated
    assert len(result.rows) == 2


async def test_writes_are_refused_before_connecting(connector: DatabricksConnector) -> None:
    with pytest.raises(ReadOnlyViolation):
        await connector.run_read_only_sql(f"DELETE FROM {_ORDERS}")
