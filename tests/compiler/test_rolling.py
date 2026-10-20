"""Cumulative metrics evaluated per calendar day: trailing windows and period-to-date totals.

Every test executes the compiled SQL, on DuckDB and SQLite. The oracle is the plain
``revenue`` metric queried per day and partition, windowed in Python over a dense calendar,
so a window is checked against the number it has to equal and not against a second copy of
the compiler's own logic.

The data crosses month boundaries, has days without orders inside a window, a partition that
is missing from whole windows, and an order without a date, which must never be counted.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import QueryMetadata
from canonic.exc import UnsupportedMeasure, ValidationFailed
from canonic.semantic.duration import Duration, add_to_date, floor_date
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.compiler.result import CompileResult

Row = dict[str, Any]

_ORDERS = [
    (1, "2025-01-30", "a", "10.00"),
    (2, "2025-01-31", "b", "5.00"),
    (3, "2025-02-02", "a", "7.00"),
    (4, "2025-02-10", "a", "1.00"),
    (5, "2025-02-11", "b", "2.00"),
    (6, "2025-03-01", "a", "3.00"),
    (7, "2025-03-15", "b", "4.00"),
    (8, None, "a", "100.00"),
]
_ORDER_ITEMS = [(1, 1, "x"), (2, 1, "y"), (3, 3, "x"), (4, 5, "y"), (5, 7, "x")]


def _sources() -> list[SemanticSource]:
    orders = SemanticSource(
        name="orders",
        connection="wh",
        table="orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="int", nullable=False),
            Column(name="order_date", type="date", nullable=True),
            Column(name="category", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
        ],
        measures=[
            Measure(name="revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="largest", expr="max(amount)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="order_day", column="order_date", granularity="day"),
            Dimension(name="order_week", column="order_date", granularity="week"),
            Dimension(name="order_month", column="order_date", granularity="month"),
            Dimension(name="order_year", column="order_date", granularity="year"),
            Dimension(name="order_ts", column="order_date"),
            Dimension(name="order_hour", column="order_date", granularity="hour"),
            Dimension(name="category", column="category"),
        ],
        joins=[
            Join(
                to="order_items",
                on="orders.order_id = order_items.order_id",
                relationship=Relationship.ONE_TO_MANY,
            )
        ],
    )
    order_items = SemanticSource(
        name="order_items",
        connection="wh",
        table="order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="int", nullable=False),
            Column(name="order_id", type="int", nullable=False),
            Column(name="sku", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="sku", column="sku")],
    )
    return [orders, order_items]


def _cumulative(metric: str, *, measure: str = "revenue", **frame: Any) -> MetricBinding:
    return MetricBinding(
        metric=metric,
        canonical=CanonicalRef(
            kind="cumulative", source="orders", measure=measure, order_by=["order_day"], **frame
        ),
    )


def _resolver() -> ContractResolver:
    return ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
            ),
            MetricBinding(
                metric="largest", canonical=CanonicalRef(source="orders", measure="largest")
            ),
            _cumulative("revenue_7d", window="7 days"),
            _cumulative("revenue_30d", window="30 days"),
            _cumulative("revenue_1m", window="1 month"),
            _cumulative("revenue_30d_first", window="30 days", period_agg="first"),
            _cumulative("revenue_30d_avg", window="30 days", period_agg="average"),
            _cumulative("revenue_mtd", grain_to_date="month"),
            _cumulative("revenue_ytd", grain_to_date="year"),
            _cumulative("revenue_to_date_first", period_agg="first"),
            _cumulative("largest_7d", measure="largest", window="7 days"),
            _cumulative("revenue_running"),
        ],
        guardrails=(),
        finality=(),
        assertions=(),
    )


def _normalize(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        return value[:10]
    if isinstance(value, (Decimal, float)):
        return round(float(value), 2)
    return value


def _duckdb() -> tuple[Any, str]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (order_id INT, order_date DATE, category TEXT, amount DECIMAL(10, 2))"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", _ORDERS)
    con.execute("CREATE TABLE order_items (item_id INT, order_id INT, sku TEXT)")
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?)", _ORDER_ITEMS)
    return con, "duckdb"


def _sqlite() -> tuple[Any, str]:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE orders (order_id INTEGER, order_date TEXT, category TEXT, amount REAL)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", _ORDERS)
    con.execute("CREATE TABLE order_items (item_id INTEGER, order_id INTEGER, sku TEXT)")
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?)", _ORDER_ITEMS)
    return con, "sqlite"


def _runner(con: Any, dialect: str) -> Callable[..., tuple[CompileResult, list[Row]]]:
    def run(
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: list[str] | None = None,
        *,
        dedup: bool = True,
        **query: Any,
    ) -> tuple[CompileResult, list[Row]]:
        result = compile(
            SemanticQuery(
                metrics=metrics, dimensions=dimensions or [], filters=filters or [], **query
            ),
            _resolver(),
            _sources(),
            connection_dialects={"wh": dialect},
            _dedup_leaves=dedup,
        )
        cursor = con.execute(result.sql)
        names = [d[0] for d in cursor.description]
        rows = [
            {name: _normalize(v) for name, v in zip(names, row, strict=True)}
            for row in cursor.fetchall()
        ]
        return result, sorted(rows, key=_sort_key)

    return run


def _sort_key(row: Row) -> tuple[Any, ...]:
    return tuple((v is None, str(v)) for v in row.values())


@pytest.fixture(params=["duckdb", "sqlite"])
def run(request: pytest.FixtureRequest) -> Callable[..., tuple[CompileResult, list[Row]]]:
    con, dialect = _duckdb() if request.param == "duckdb" else _sqlite()
    return _runner(con, dialect)


def _daily(
    run: Any, partitions: list[str], metric: str = "revenue"
) -> dict[tuple[Any, ...], float]:
    """The base metric per day and partition, the input of every window."""
    _, rows = run([metric], ["order_day", *partitions])
    return {
        (date.fromisoformat(row["order_day"]), *(row[p] for p in partitions)): row[metric]
        for row in rows
        if row["order_day"] is not None
    }


def _oracle(
    daily: dict[tuple[Any, ...], float],
    lower: Callable[[date], date | None],
    *,
    last_day: date | None = None,
    combine: Callable[[list[float]], float] = sum,
) -> dict[tuple[Any, ...], float]:
    """The window value per calendar day and partition, for days whose window is not empty.

    ``lower(t)`` is the first day inside the window of ``t``, ``None`` for no lower bound. Days
    run on past the last row for as long as a window can reach back to it, up to ``last_day``.
    An unbounded frame never empties, so it stops at the last row.
    """
    days = [key[0] for key in daily]
    first, last = min(days), max(days)
    if lower(last) is not None:
        last += timedelta(days=400)
    if last_day is not None:
        last = min(last, last_day)
    partitions = {key[1:] for key in daily}
    values: dict[tuple[Any, ...], float] = {}
    day = first
    while day <= last:
        start = lower(day)
        for partition in partitions:
            inside = [
                v
                for (d, *p), v in daily.items()
                if tuple(p) == partition and d <= day and (start is None or d >= start)
            ]
            if inside:
                values[(day, *partition)] = round(combine(inside), 2)
        day += timedelta(days=1)
    return values


def _trailing(n: int, unit: str = "day") -> Callable[[date], date]:
    duration = Duration.parse(f"{n} {unit}")
    return lambda day: add_to_date(day, duration, sign=-1) + timedelta(days=1)


def _by_day(rows: list[Row], metric: str, partitions: list[str]) -> dict[tuple[Any, ...], float]:
    return {
        (date.fromisoformat(row["order_day"]), *(row[p] for p in partitions)): row[metric]
        for row in rows
        if row[metric] is not None
    }


class TestTrailingWindow:
    def test_w1_seven_days_by_day_includes_days_without_rows(self, run: Any) -> None:
        _, rows = run(["revenue_7d"], ["order_day"])
        expected = _oracle(_daily(run, []), _trailing(7))
        assert _by_day(rows, "revenue_7d", []) == expected
        # 2025-02-08 has no order, but its window still holds the order of 2025-02-02.
        assert expected[(date(2025, 2, 8),)] == 7.0

    def test_w2_a_day_with_an_empty_window_has_no_row(self, run: Any) -> None:
        _, rows = run(["revenue_7d", "revenue"], ["order_day"])
        days = {row["order_day"]: row for row in rows}
        assert "2025-02-20" not in days  # nothing between 2025-02-14 and 2025-02-20
        assert days["2025-03-01"]["revenue_7d"] == 3.0
        assert days["2025-02-05"]["revenue"] is None

    def test_w6_each_partition_has_its_own_window(self, run: Any) -> None:
        _, rows = run(["revenue_7d"], ["order_day", "category"])
        expected = _oracle(_daily(run, ["category"]), _trailing(7))
        assert _by_day(rows, "revenue_7d", ["category"]) == expected
        assert (date(2025, 2, 6), "b") in expected  # b's last order is 2025-01-31
        assert (date(2025, 2, 7), "b") not in expected

    def test_calendar_month_window_clamps_to_month_end(self, run: Any) -> None:
        _, rows = run(["revenue_1m"], ["order_day"])
        expected = _oracle(_daily(run, []), _trailing(1, "month"))
        assert _by_day(rows, "revenue_1m", []) == expected
        # (2025-02-28 - 1 month, 2025-02-28] starts after 2025-01-28.
        assert expected[(date(2025, 2, 28),)] == 25.0

    def test_min_and_max_measures_re_aggregate_with_themselves(self, run: Any) -> None:
        _, rows = run(["largest_7d"], ["order_day"])
        expected = _oracle(_daily(run, [], "largest"), _trailing(7), combine=max)
        assert _by_day(rows, "largest_7d", []) == expected

    def test_w11_fanning_partition_matches_the_plain_metric(self, run: Any) -> None:
        _, rows = run(["revenue_7d"], ["order_day", "sku"])
        expected = _oracle(_daily(run, ["sku"]), _trailing(7))
        assert _by_day(rows, "revenue_7d", ["sku"]) == expected


class TestPeriodToDate:
    def test_w3_month_to_date_restarts_on_the_first(self, run: Any) -> None:
        _, rows = run(["revenue_mtd"], ["order_day"])
        expected = _oracle(_daily(run, []), lambda day: day.replace(day=1))
        assert _by_day(rows, "revenue_mtd", []) == expected
        assert expected[(date(2025, 1, 31),)] == 15.0
        assert expected[(date(2025, 2, 2),)] == 7.0

    def test_w4_year_to_date_by_month_is_the_running_sum_within_the_year(self, run: Any) -> None:
        _, ytd = run(["revenue_ytd"], ["order_month"])
        _, monthly = run(["revenue"], ["order_month"])
        by_month = {row["order_month"]: row["revenue"] for row in monthly}
        running, total = {}, 0.0
        for month in range(1, 13):
            key = date(2025, month, 1).isoformat()
            total += by_month.get(key) or 0.0
            running[key] = round(total, 2)
        # The year is not over after the last order, so its total runs on to December.
        assert {row["order_month"]: row["revenue_ytd"] for row in ytd} == running


class TestCoarserBuckets:
    @pytest.mark.parametrize(
        ("metric", "pick"),
        [("revenue_30d", max), ("revenue_30d_first", min)],
    )
    def test_w5_first_and_last_day_of_the_month(self, run: Any, metric: str, pick: Any) -> None:
        _, rows = run([metric], ["order_month"])
        values = _oracle(_daily(run, []), _trailing(30))
        spine = sorted({key[0] for key in values} | {date(2025, 1, 30), date(2025, 3, 15)})
        spine = [spine[0] + timedelta(days=i) for i in range((spine[-1] - spine[0]).days + 1)]
        expected: dict[str, float] = {}
        for month in sorted({floor_date(day, Duration.parse("1m").unit) for day in spine}):
            target = pick(day for day in spine if day.replace(day=1) == month)
            if (target,) in values:
                expected[month.isoformat()] = values[(target,)]
        assert {row["order_month"]: row[metric] for row in rows} == expected

    def test_w5_average_over_the_days_of_the_month(self, run: Any) -> None:
        _, rows = run(["revenue_30d_avg"], ["order_month"])
        values = _oracle(_daily(run, []), _trailing(30))
        expected: dict[str, list[float]] = {}
        for (day,), value in values.items():
            expected.setdefault(day.replace(day=1).isoformat(), []).append(value)
        assert {row["order_month"]: row["revenue_30d_avg"] for row in rows} == {
            month: round(sum(v) / len(v), 2) for month, v in expected.items()
        }

    def test_unbounded_first_day_of_the_month(self, run: Any) -> None:
        _, rows = run(["revenue_to_date_first"], ["order_month"])
        # The first spine day of January is 2025-01-30, the first day with data.
        values = {row["order_month"]: row["revenue_to_date_first"] for row in rows}
        assert values == {"2025-01-01": 10.0, "2025-02-01": 15.0, "2025-03-01": 28.0}

    def test_w10_shares_rows_with_a_plain_metric_on_the_same_dimension(self, run: Any) -> None:
        _, rows = run(["revenue_30d", "revenue"], ["order_month"])
        by_month = {row["order_month"]: (row["revenue_30d"], row["revenue"]) for row in rows}
        for month in ("2025-01-01", "2025-02-01", "2025-03-01"):
            assert None not in by_month[month]
        # The window of 2025-03-15 reaches into April, which has no order of its own.
        assert by_month["2025-04-01"] == (4.0, None)
        # The order without a date has a month of NULL and no window.
        assert by_month[None] == (None, 100.0)

    def test_a_week_dimension_on_the_order_column_sets_the_grain(self, run: Any) -> None:
        _, rows = run(["revenue_mtd"], ["order_week"])
        values = _oracle(_daily(run, []), lambda day: day.replace(day=1))
        weeks: dict[str, date] = {}
        for (day,) in values:
            week = (day - timedelta(days=day.weekday())).isoformat()
            weeks[week] = max(weeks.get(week, day), day)
        assert {row["order_week"]: row["revenue_mtd"] for row in rows} == {
            week: values[(day,)] for week, day in weeks.items()
        }


class TestFilters:
    def test_w7_an_order_filter_only_decides_which_days_are_shown(self, run: Any) -> None:
        _, unfiltered = run(["revenue_7d"], ["order_day"])
        _, rows = run(["revenue_7d"], ["order_day"], ["order_day >= '2025-02-03'"])
        # The orders of 2025-01-30 and 2025-01-31 are filtered from view, not from the window.
        assert rows[0] == {"order_day": "2025-02-03", "revenue_7d": 22.0}
        assert rows == [row for row in unfiltered if row["order_day"] >= "2025-02-03"]

    def test_w8_a_partition_filter_narrows_the_rows(self, run: Any) -> None:
        _, unfiltered = run(["revenue_7d"], ["order_day", "category"])
        _, rows = run(["revenue_7d"], ["order_day", "category"], ["category = 'a'"])
        assert rows == [row for row in unfiltered if row["category"] == "a"]

    def test_w9_a_filter_on_order_and_partition_columns_is_refused(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="reads order and non-order columns"):
            run(["revenue_7d"], ["order_day"], ["order_date > '2025-02-01' OR category = 'a'"])

    def test_metadata_lists_the_order_filter_as_visibility(self, run: Any) -> None:
        result, _ = run(["revenue_7d"], ["order_day", "category"], ["order_day >= '2025-02-03'"])
        assert result.resolved == {"revenue_7d": "cumulative(orders.revenue, window=7 days)"}
        metadata = QueryMetadata.from_compile_result(result).cumulative
        assert metadata is not None
        assert metadata.model_dump() == {
            "metric": "revenue_7d",
            "order_by": ["order_day"],
            "partition_by": ["category"],
            "on_gap": "skip",
            "history": "exclude",
            "visibility_filters": ["order_day >= '2025-02-03'"],
            "window": "7 days",
            "grain_to_date": None,
            "period_agg": "last",
            "lookback": "7 days",
        }


class TestTimeRange:
    def test_w13_last_days_compile_to_literal_bounds(self, run: Any) -> None:
        as_of = datetime(2025, 2, 12, 9, 30)
        result, rows = run(
            ["revenue"],
            ["order_day"],
            time_range={"dimension": "order_day", "last": "14 days"},
            as_of=as_of,
        )
        _, filtered = run(
            ["revenue"],
            ["order_day"],
            ["order_day >= '2025-01-29'", "order_day < '2025-02-12'"],
        )
        assert rows == filtered
        assert "2025-01-29" in result.sql and "CURRENT_DATE" not in result.sql.upper()
        metadata = QueryMetadata.from_compile_result(result).time_range
        assert metadata is not None
        assert metadata.model_dump() == {
            "dimension": "order_day",
            "start": "2025-01-29",
            "end": "2025-02-12",
            "last": "14 days",
            "include_current": False,
            "anchor": "2025-02-12T09:30:00",
        }

    def test_w14_the_first_day_of_the_range_carries_its_full_window(self, run: Any) -> None:
        _, unbounded = run(["revenue_7d"], ["order_day"])
        _, rows = run(
            ["revenue_7d"],
            ["order_day"],
            time_range={"dimension": "order_day", "start": "2025-02-03", "end": "2025-02-12"},
        )
        assert rows == [row for row in unbounded if "2025-02-03" <= row["order_day"] < "2025-02-12"]
        assert rows[0]["revenue_7d"] == 22.0

    def test_month_to_date_reads_back_to_the_start_of_the_month(self, run: Any) -> None:
        result, rows = run(
            ["revenue_mtd"],
            ["order_day"],
            time_range={"dimension": "order_day", "start": "2025-02-10", "end": "2025-02-12"},
        )
        assert rows == [
            {"order_day": "2025-02-10", "revenue_mtd": 8.0},
            {"order_day": "2025-02-11", "revenue_mtd": 10.0},
        ]
        assert "2025-02-01" in result.sql

    def test_no_day_after_the_anchor_is_shown(self, run: Any) -> None:
        _, rows = run(["revenue_7d"], ["order_day"], as_of=datetime(2025, 2, 12))
        assert rows[-1]["order_day"] == "2025-02-12"

    def test_a_range_on_a_non_time_dimension_is_refused(self, run: Any) -> None:
        with pytest.raises(ValidationFailed, match="must be date or timestamp"):
            run(["revenue"], [], time_range={"dimension": "category", "last": "7 days"})


class TestRefusals:
    def test_the_order_column_must_be_grouped(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="another dimension on column 'order_date'"):
            run(["revenue_7d"], ["category"])

    def test_a_grain_finer_than_a_day_is_refused(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="evaluated per day"):
            run(["revenue_7d"], ["order_hour"])

    def test_a_plain_date_column_counts_as_a_day(self, run: Any) -> None:
        _, by_column = run(["revenue_7d"], ["order_ts"])
        _, by_day = run(["revenue_7d"], ["order_day"])
        assert [list(r.values()) for r in by_column] == [list(r.values()) for r in by_day]

    def test_two_grains_of_the_order_column_are_refused(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="request one of them"):
            run(["revenue_7d"], ["order_day", "order_month"])


class TestCompilation:
    def test_w15_an_unbounded_binding_keeps_its_running_total_window(self, run: Any) -> None:
        result, _ = run(["revenue_running"], ["order_day"])
        assert "_spine" not in result.sql and "ROWS BETWEEN UNBOUNDED PRECEDING" in result.sql

    def test_two_windows_on_one_measure_never_share_a_leaf(self, run: Any) -> None:
        _, together = run(["revenue_7d", "revenue_30d"], ["order_day"])
        _, apart = run(["revenue_7d", "revenue_30d"], ["order_day"], dedup=False)
        assert together == apart
        _, seven = run(["revenue_7d"], ["order_day"])
        assert {r["order_day"]: r["revenue_7d"] for r in together if r["revenue_7d"]} == {
            r["order_day"]: r["revenue_7d"] for r in seven
        }
