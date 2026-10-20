"""Date arithmetic and the day spine, rendered per dialect and executed where an engine is local.

Every engine but SQLite clamps month arithmetic to the end of the month. SQLite rolls over
(``DATE('2026-01-31', '+1 month')`` is ``2026-03-03``), so its adapter builds the clamp
itself. Both local engines are checked against the Python reference in
:func:`canonic.semantic.duration.add_to_date`.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

import duckdb
import pytest
from sqlglot import exp

from canonic.compiler._helpers import parse_dialect
from canonic.compiler.dialect import DIALECT_ADAPTERS
from canonic.compiler.spine import MAX_SPINE_DAYS, bounds_select, digits_select, spine_select
from canonic.semantic.duration import Duration, DurationUnit, add_to_date

_STEPS = [
    (date(2026, 1, 31), 1, DurationUnit.MONTH),
    (date(2024, 1, 31), 1, DurationUnit.MONTH),
    (date(2024, 2, 29), 1, DurationUnit.YEAR),
    (date(2026, 3, 31), -1, DurationUnit.MONTH),
    (date(2026, 5, 31), -1, DurationUnit.QUARTER),
    (date(2026, 12, 31), 2, DurationUnit.MONTH),
    (date(2026, 3, 1), -7, DurationUnit.DAY),
    (date(2026, 1, 15), 2, DurationUnit.WEEK),
]


def _date(text: str) -> exp.Expression:
    return exp.Cast(this=exp.Literal.string(text), to=exp.DataType.build("DATE"))


def _execute(dialect: str, sql: str) -> list[tuple[Any, ...]]:
    if dialect == "sqlite":
        return sqlite3.connect(":memory:").execute(sql).fetchall()
    return duckdb.sql(sql).fetchall()


@pytest.mark.parametrize("dialect", ["sqlite", "duckdb"])
@pytest.mark.parametrize(("day", "amount", "unit"), _STEPS)
def test_date_add_matches_the_python_reference(
    dialect: str, day: date, amount: int, unit: DurationUnit
) -> None:
    adapter = DIALECT_ADAPTERS[dialect]
    sql = adapter.emit(
        exp.select(exp.alias_(adapter.date_add(_date(day.isoformat()), amount, unit), "x"))
    )
    expected = add_to_date(day, Duration(abs(amount), unit), sign=1 if amount > 0 else -1)
    assert str(_execute(dialect, sql)[0][0]) == expected.isoformat()


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgres", 'CAST("t"."d" + INTERVAL \'-1 MONTH\' AS DATE)'),
        ("redshift", 'CAST(DATEADD(MONTH, -1, "t"."d") AS DATE)'),
        ("duckdb", 'CAST("t"."d" + INTERVAL (-1) MONTH AS DATE)'),
        ("snowflake", 'CAST(DATEADD(MONTH, -1, "t"."d") AS DATE)'),
        ("databricks", "CAST(DATEADD(MONTH, -1, `t`.`d`) AS DATE)"),
        ("mysql", "CAST(DATE_ADD(`t`.`d`, INTERVAL -1 MONTH) AS DATE)"),
        ("clickhouse", 'CAST(DATE_ADD(MONTH, -1, "t"."d") AS Nullable(DATE))'),
    ],
)
def test_date_add_uses_each_engines_native_month_arithmetic(dialect: str, expected: str) -> None:
    adapter = DIALECT_ADAPTERS[dialect]
    step = adapter.date_add(exp.column("d", table="t"), -1, DurationUnit.MONTH)
    assert adapter.emit(exp.select(step)) == f"SELECT {expected}"


def test_a_computed_step_is_days_only() -> None:
    adapter = DIALECT_ADAPTERS["postgres"]
    with pytest.raises(ValueError, match="must be in days"):
        adapter.date_add(exp.column("d"), exp.column("n"), DurationUnit.MONTH)


@pytest.mark.parametrize(
    ("day", "expected"),
    [("2026-02-14", "2026-01-01"), ("2026-05-31", "2026-04-01"), ("2026-12-01", "2026-10-01")],
)
def test_sqlite_truncates_to_the_quarter(day: str, expected: str) -> None:
    adapter = DIALECT_ADAPTERS["sqlite"]
    trunc = exp.DateTrunc(this=exp.Literal.string(day), unit=exp.Literal.string("quarter"))
    assert _execute("sqlite", adapter.emit(exp.select(trunc)))[0][0] == expected


def _spine(dialect: str, days: list[str], cap: str) -> list[str]:
    with parse_dialect(dialect):
        rows = [exp.select(exp.alias_(_date(d), "_day")) for d in days]
        daily: exp.Expression = rows[0]
        for row in rows[1:]:
            daily = exp.union(daily, row, distinct=False)
        query = (
            exp.select("_day")
            .from_("spine")
            .order_by("_day")
            .with_("daily", as_=daily)
            .with_("digits", as_=digits_select())
            .with_("bounds", as_=bounds_select("daily", _date(cap)))
            .with_("spine", as_=spine_select("bounds", "digits"))
        )
        sql = DIALECT_ADAPTERS[dialect].emit(query)
    return [str(row[0]) for row in _execute(dialect, sql)]


@pytest.mark.parametrize("dialect", ["sqlite", "duckdb"])
def test_spine_has_every_day_between_the_bounds_and_stops_at_the_cap(dialect: str) -> None:
    assert _spine(dialect, ["2024-02-27", "2024-03-03"], "2024-03-02") == [
        "2024-02-27",
        "2024-02-28",
        "2024-02-29",
        "2024-03-01",
        "2024-03-02",
    ]


@pytest.mark.parametrize("dialect", ["sqlite", "duckdb"])
def test_spine_keeps_the_latest_days_when_the_data_spans_more_than_it_holds(dialect: str) -> None:
    last = date(2026, 1, 1)
    days = _spine(dialect, ["1700-01-01", last.isoformat()], last.isoformat())
    assert len(days) == MAX_SPINE_DAYS
    assert days[0] == (last - timedelta(days=MAX_SPINE_DAYS - 1)).isoformat()
    assert days[-1] == last.isoformat()
