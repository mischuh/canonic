"""Smoke tests for the Snowflake connector against fakesnow, an in-process emulator.

fakesnow (https://github.com/tekumara/fakesnow) patches ``snowflake.connector`` and runs
the SQL on DuckDB, so these tests need no account or network. It is not Snowflake: it is
used to check connect, query execution, type normalization and column introspection, and
is skipped when it is not installed. Three emulator gaps are deliberately not asserted:
``SHOW ... KEYS IN DATABASE`` is not implemented and ``SHOW IMPORTED KEYS`` returns wrong
rows, and ``INFORMATION_SCHEMA.TABLES`` omits views. Declared keys, view discovery,
identifier case and key-pair auth need the live ``integration`` test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.config import Connection
from canonic.connectors.snowflake import SnowflakeConnector

if TYPE_CHECKING:
    from collections.abc import Iterator

fakesnow = pytest.importorskip("fakesnow")
snowflake_connector = pytest.importorskip("snowflake.connector")

_SEED = [
    "CREATE SCHEMA IF NOT EXISTS ANALYTICS.PUBLIC",
    "CREATE TABLE ANALYTICS.PUBLIC.CUSTOMERS (ID NUMBER, NAME VARCHAR)",
    "CREATE TABLE ANALYTICS.PUBLIC.ORDERS "
    "(ID NUMBER, CUSTOMER_ID NUMBER, AMOUNT NUMBER(18,2), CREATED_AT TIMESTAMP_NTZ)",
    "CREATE VIEW ANALYTICS.PUBLIC.V_ORDERS AS SELECT * FROM ANALYTICS.PUBLIC.ORDERS",
    "INSERT INTO ANALYTICS.PUBLIC.CUSTOMERS VALUES (1, 'a'), (2, 'b')",
    "INSERT INTO ANALYTICS.PUBLIC.ORDERS VALUES "
    "(1, 1, 10.50, '2026-01-02 03:04:05'), (2, 2, 20.00, '2026-02-03 00:00:00')",
]


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch) -> Iterator[SnowflakeConnector]:
    monkeypatch.setenv("SF_FAKE_PASSWORD", "unused")
    with fakesnow.patch():
        seed = snowflake_connector.connect(database="ANALYTICS", schema="PUBLIC")
        cursor = seed.cursor()
        for statement in _SEED:
            cursor.execute(statement)
        seed.close()
        yield SnowflakeConnector(
            Connection(
                id="fake",
                type="snowflake",
                params={
                    "account": "fake",
                    "user": "canonic",
                    "warehouse": "WH",
                    "database": "ANALYTICS",
                },
                credentials_ref="env:SF_FAKE_PASSWORD",
            )
        )


async def test_connection_ok(connector: SnowflakeConnector) -> None:
    assert (await connector.test_connection()).status == "ok"


async def test_query_returns_typed_rows(connector: SnowflakeConnector) -> None:
    result = await connector.run_read_only_sql(
        'SELECT "ID", "AMOUNT", "CREATED_AT" FROM "ANALYTICS"."PUBLIC"."ORDERS" ORDER BY 1'
    )
    assert [(c.name, c.type) for c in result.columns] == [
        ("ID", "int"),
        ("AMOUNT", "decimal"),
        ("CREATED_AT", "timestamp"),
    ]
    assert [row[0] for row in result.rows] == [1, 2]


async def test_percentile_and_date_trunc_execute(connector: SnowflakeConnector) -> None:
    percentile = await connector.run_read_only_sql(
        'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "AMOUNT") AS P '
        'FROM "ANALYTICS"."PUBLIC"."ORDERS"'
    )
    assert str(percentile.rows[0][0]) == "15.25"

    monthly = await connector.run_read_only_sql(
        'SELECT DATE_TRUNC(\'MONTH\', "CREATED_AT") AS M FROM "ANALYTICS"."PUBLIC"."ORDERS" '
        "WHERE \"CREATED_AT\" >= CURRENT_DATE - INTERVAL '3000 DAY' GROUP BY 1 ORDER BY 1"
    )
    assert [row[0].month for row in monthly.rows] == [1, 2]


async def test_describe_table_and_view(connector: SnowflakeConnector) -> None:
    for relation in ("PUBLIC.ORDERS", "PUBLIC.V_ORDERS"):
        columns = await connector.describe_relation(relation)
        assert [(c.name, c.type) for c in columns] == [
            ("ID", "int"),
            ("CUSTOMER_ID", "int"),
            ("AMOUNT", "decimal"),
            ("CREATED_AT", "timestamp"),
        ]


async def test_introspection_returns_columns_without_keys(connector: SnowflakeConnector) -> None:
    by_relation = {s.relation: s for s in await connector.introspect_schema()}

    assert {"PUBLIC.ORDERS", "PUBLIC.CUSTOMERS"} <= set(by_relation)
    assert by_relation["PUBLIC.ORDERS"].kind == "table"
    orders = by_relation["PUBLIC.ORDERS"]
    assert [(c.name, c.type) for c in orders.columns] == [
        ("ID", "int"),
        ("CUSTOMER_ID", "int"),
        ("AMOUNT", "decimal"),
        ("CREATED_AT", "timestamp"),
    ]
