"""Compiler tests for JSON-path dimensions (``json_path`` on ``dimensions``).

A ``json_path`` dimension reads one key of a JSON column from key segments, so a key such as
``$current_url`` or ``a.b`` is never mistaken for path syntax. The compiled SQL is checked per
dialect, and executed on DuckDB and SQLite so the numbers are pinned and not only the text.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

_ROWS = [
    (
        1,
        10,
        '{"$current_url": "/pricing", "plan": "pro", "seats": 3, "address": {"city": "Berlin"}}',
    ),
    (
        2,
        20,
        '{"$current_url": "/pricing", "plan": "free", "seats": 1, "address": {"city": "Berlin"}}',
    ),
    (3, 30, '{"$current_url": "/docs", "plan": "pro", "seats": 5, "address": {"city": "Paris"}}'),
    (4, 40, '{"plan": "pro"}'),
]


@pytest.fixture
def events() -> SemanticSource:
    return SemanticSource(
        name="events",
        connection="warehouse",
        table="events",
        grain=["id"],
        columns=[
            Column(name="id", type="int", nullable=False),
            Column(name="amount", type="int", nullable=False),
            Column(name="properties", type="json", nullable=True),
        ],
        measures=[Measure(name="total_amount", expr="sum(amount)")],
        dimensions=[
            Dimension(name="url", column="properties", json_path=["$current_url"], type="string"),
            Dimension(
                name="city", column="properties", json_path=["address", "city"], type="string"
            ),
            Dimension(name="seats", column="properties", json_path=["seats"], type="int"),
        ],
    )


@pytest.fixture
def resolver() -> ContractResolver:
    binding = MetricBinding(
        metric="total_amount",
        canonical=CanonicalRef(source="events", measure="total_amount"),
    )
    return ContractResolver(bindings=[binding], guardrails=[])


def _sql(events: SemanticSource, resolver: ContractResolver, dialect: str, **query: Any) -> str:
    return compile(
        SemanticQuery(metrics=["total_amount"], **query),
        resolver,
        [events],
        connection_dialects={"warehouse": dialect},
    ).sql


@pytest.mark.parametrize(
    ("dialect", "fragment"),
    [
        ("postgres", '"events"."properties" ->> \'$current_url\''),
        ("duckdb", '"events"."properties" ->> \'$."$current_url"\''),
        ("sqlite", 'JSON_EXTRACT("events"."properties", \'$."$current_url"\')'),
        ("snowflake", 'GET_PATH("events"."properties", \'["$current_url"]\')'),
        ("mysql", '"events"."properties" ->> \'$."$current_url"\''),
        ("clickhouse", 'JSONExtractString("events"."properties", \'$current_url\')'),
        ("databricks", 'GET_JSON_OBJECT("events"."properties", \'$["$current_url"]\')'),
        ("redshift", 'JSON_EXTRACT_PATH_TEXT("events"."properties", \'$current_url\')'),
    ],
)
def test_dollar_prefixed_key_renders_as_one_key_per_dialect(
    events, resolver, dialect, fragment
) -> None:
    sql = _sql(events, resolver, dialect, dimensions=["url"])
    # MySQL renders identifiers with backticks, the rest with double quotes.
    assert fragment.replace('"events"."properties"', _quoted(dialect)) in sql


def _quoted(dialect: str) -> str:
    return (
        "`events`.`properties`" if dialect in {"mysql", "databricks"} else '"events"."properties"'
    )


def test_postgres_nested_keys_chain_arrows_that_work_on_jsonb(events, resolver) -> None:
    sql = _sql(events, resolver, "postgres", dimensions=["city"])
    assert "\"events\".\"properties\" -> 'address' ->> 'city'" in sql
    assert "JSON_EXTRACT_PATH" not in sql.upper()


def test_non_text_type_casts_the_extracted_value(events, resolver) -> None:
    assert 'CAST("events"."properties" ->> \'seats\' AS BIGINT)' in _sql(
        events, resolver, "postgres", dimensions=["seats"]
    )


def _run_duckdb(sql: str) -> list[tuple[Any, ...]]:
    conn = duckdb.connect()
    conn.execute("CREATE TABLE events (id INTEGER, amount INTEGER, properties JSON)")
    conn.executemany("INSERT INTO events VALUES (?, ?, ?)", _ROWS)
    return conn.execute(sql).fetchall()


def _run_sqlite(sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE events (id INTEGER, amount INTEGER, properties TEXT)")
    conn.executemany("INSERT INTO events VALUES (?, ?, ?)", _ROWS)
    return conn.execute(sql).fetchall()


_ENGINES = [("duckdb", _run_duckdb), ("sqlite", _run_sqlite)]


def _by_key(rows: list[tuple[Any, ...]]) -> dict[Any, Any]:
    return {key: total for key, total in rows}


@pytest.mark.parametrize(("dialect", "run"), _ENGINES)
def test_dollar_prefixed_key_groups_to_the_right_numbers(events, resolver, dialect, run) -> None:
    rows = run(_sql(events, resolver, dialect, dimensions=["url"]))
    assert _by_key(rows) == {"/pricing": 30, "/docs": 30, None: 40}


@pytest.mark.parametrize(("dialect", "run"), _ENGINES)
def test_nested_key_groups_to_the_right_numbers(events, resolver, dialect, run) -> None:
    rows = run(_sql(events, resolver, dialect, dimensions=["city"]))
    assert _by_key(rows) == {"Berlin": 30, "Paris": 30, None: 40}


@pytest.mark.parametrize(("dialect", "run"), _ENGINES)
def test_filter_on_a_typed_json_dimension_compares_numerically(
    events, resolver, dialect, run
) -> None:
    """``seats`` is cast to an int, so ``>= 3`` is numeric and 10 would not sort before 3."""
    rows = run(_sql(events, resolver, dialect, filters=["seats >= 3"]))
    assert rows == [(40,)]


@pytest.mark.parametrize(("dialect", "run"), _ENGINES)
def test_filter_on_a_string_json_dimension(events, resolver, dialect, run) -> None:
    rows = run(_sql(events, resolver, dialect, filters=["url = '/docs'"]))
    assert rows == [(30,)]
