"""Live tests for the Snowflake connector against a real account (``integration``).

Skipped unless ``CANONIC_TEST_SNOWFLAKE_ACCOUNT`` is set. One-time setup: run
``fixtures/snowflake_live_setup.sql`` in a Snowsight worksheet as ACCOUNTADMIN. It creates
the ``CANONIC_TEST.SHOP`` data these tests assert on, and a ``CANONIC_RO`` role that only
holds ``SELECT``, so the ``read_only_role`` path is exercised too.

Environment:
    CANONIC_TEST_SNOWFLAKE_ACCOUNT, _USER, _WAREHOUSE   required
    CANONIC_TEST_SNOWFLAKE_PRIVATE_KEY_PATH             key-pair auth (PEM, PKCS8)
    CANONIC_TEST_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE       optional, for an encrypted key
    CANONIC_TEST_SNOWFLAKE_PASSWORD                     password auth, when no key path is set
    CANONIC_TEST_SNOWFLAKE_DATABASE                     default CANONIC_TEST
    CANONIC_TEST_SNOWFLAKE_ROLE                         default CANONIC_RO
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from canonic.config import Connection
from canonic.connectors.snowflake import SnowflakeConnector
from canonic.exc import ReadOnlyViolation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CANONIC_TEST_SNOWFLAKE_ACCOUNT"),
        reason="set CANONIC_TEST_SNOWFLAKE_{ACCOUNT,USER,WAREHOUSE} and an auth variable to run",
    ),
]

_PREFIX = "CANONIC_TEST_SNOWFLAKE_"
_ROLE = os.environ.get(f"{_PREFIX}ROLE", "CANONIC_RO")
_DATABASE = os.environ.get(f"{_PREFIX}DATABASE", "CANONIC_TEST")
_ORDERS = f'"{_DATABASE}"."SHOP"."ORDERS"'


def _connector(**extra_params: Any) -> SnowflakeConnector:
    params: dict[str, Any] = {
        "account": os.environ[f"{_PREFIX}ACCOUNT"],
        "user": os.environ[f"{_PREFIX}USER"],
        "warehouse": os.environ[f"{_PREFIX}WAREHOUSE"],
        "database": _DATABASE,
        "schemas": ["SHOP"],
        **extra_params,
    }
    if key_path := os.environ.get(f"{_PREFIX}PRIVATE_KEY_PATH"):
        params["private_key_path"] = key_path
        has_passphrase = bool(os.environ.get(f"{_PREFIX}PRIVATE_KEY_PASSPHRASE"))
        ref = f"env:{_PREFIX}PRIVATE_KEY_PASSPHRASE" if has_passphrase else None
    else:
        ref = f"env:{_PREFIX}PASSWORD"
    return SnowflakeConnector(
        Connection(
            id="live", type="snowflake", params=params, credentials_ref=ref, read_only_role=_ROLE
        )
    )


@pytest.fixture
def connector() -> SnowflakeConnector:
    return _connector()


async def test_connection_uses_read_only_role(connector: SnowflakeConnector) -> None:
    assert (await connector.test_connection()).status == "ok"
    result = await connector.run_read_only_sql("SELECT CURRENT_ROLE() AS R")
    assert result.rows == [[_ROLE]]


async def test_introspection_relations_and_kinds(connector: SnowflakeConnector) -> None:
    by_relation = {s.relation: s for s in await connector.introspect_schema()}

    assert {"SHOP.ORDERS", "SHOP.CUSTOMERS", "SHOP.V_ORDERS", "SHOP.mixed_case"} <= set(by_relation)
    assert by_relation["SHOP.ORDERS"].kind == "table"
    assert by_relation["SHOP.V_ORDERS"].kind == "view"
    assert by_relation["SHOP.ORDERS"].row_count_estimate == 3


async def test_introspection_columns_and_declared_keys(connector: SnowflakeConnector) -> None:
    orders = {s.relation: s for s in await connector.introspect_schema()}["SHOP.ORDERS"]

    assert [(c.name, c.type) for c in orders.columns] == [
        ("ID", "int"),
        ("CUSTOMER_ID", "int"),
        ("AMOUNT", "decimal"),
        ("CREATED_AT", "timestamp"),
    ]
    assert orders.primary_key == ["ID"]
    assert [
        (f.columns, f.references.relation, f.references.columns) for f in orders.foreign_keys
    ] == [(["CUSTOMER_ID"], "SHOP.CUSTOMERS", ["ID"])]


async def test_identifier_case_is_preserved_and_significant(connector: SnowflakeConnector) -> None:
    by_relation = {s.relation: s for s in await connector.introspect_schema()}
    assert by_relation["SHOP.mixed_case"].columns[0].name == "lower_col"

    result = await connector.run_read_only_sql(f'SELECT SUM("AMOUNT") AS T FROM {_ORDERS}')
    assert str(result.rows[0][0]) == "60.50"
    with pytest.raises(Exception, match="does not exist or not authorized"):
        await connector.run_read_only_sql(f'SELECT * FROM "{_DATABASE}"."SHOP"."orders"')


async def test_describe_view(connector: SnowflakeConnector) -> None:
    columns = await connector.describe_relation("SHOP.V_ORDERS")
    assert [c.name for c in columns] == ["ID", "CUSTOMER_ID", "AMOUNT"]


async def test_variant_path(connector: SnowflakeConnector) -> None:
    result = await connector.run_read_only_sql(
        f'SELECT "TIER":plan::string AS P FROM "{_DATABASE}"."SHOP"."CUSTOMERS" ORDER BY "ID"'
    )
    assert result.rows == [["pro"], ["free"]]


async def test_percentile_and_date_trunc(connector: SnowflakeConnector) -> None:
    percentile = await connector.run_read_only_sql(
        f'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "AMOUNT") AS P FROM {_ORDERS}'
    )
    assert float(percentile.rows[0][0]) == 20.0

    monthly = await connector.run_read_only_sql(
        f"SELECT DATE_TRUNC('MONTH', \"CREATED_AT\") AS M, COUNT(*) AS N FROM {_ORDERS} "
        "WHERE \"CREATED_AT\" >= CURRENT_DATE - INTERVAL '3650 DAY' GROUP BY 1 ORDER BY 1"
    )
    assert [row[1] for row in monthly.rows] == [1, 2]


async def test_row_limit_truncates() -> None:
    result = await _connector(row_limit=2).run_read_only_sql(f'SELECT "ID" FROM {_ORDERS}')
    assert result.truncated
    assert len(result.rows) == 2


async def test_statement_timeout_is_applied() -> None:
    connector = _connector(statement_timeout_ms=1000)
    with pytest.raises(Exception, match="(?i)timeout"):
        await connector.run_read_only_sql("SELECT SYSTEM$WAIT(10) AS W")


async def test_write_is_rejected(connector: SnowflakeConnector) -> None:
    with pytest.raises(ReadOnlyViolation):
        await connector.run_read_only_sql(f"DELETE FROM {_ORDERS}")
