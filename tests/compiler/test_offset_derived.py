"""Offset and derived metrics: period-over-period comparisons and arithmetic over metrics.

Every test executes the compiled SQL on DuckDB and SQLite. The oracle is the plain metric
queried alone, shifted or combined in Python, so an offset is checked against the number it has
to equal and not against a second copy of the compiler's own logic.

The orders cross a year boundary, include the last day of a month (a month offset clamps it),
a customer who orders in two months (a distinct count must not add up), and an order item
join that fans out.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import QueryMetadata
from canonic.semantic.duration import Duration, add_to_date
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.compiler.result import CompileResult

Row = dict[str, Any]

_ORDERS = [
    (1, "2024-11-15", 7, "a", "10.00"),
    (2, "2024-12-31", 8, "b", "20.00"),
    (3, "2025-01-10", 7, "a", "5.00"),
    (4, "2025-01-31", 9, "b", "40.00"),
    (5, "2025-02-14", 7, "a", "8.00"),
    (6, "2025-03-03", 8, "b", "1.00"),
    (7, "2025-12-02", 9, "a", "3.00"),
    (8, "2026-01-20", 7, "b", "6.00"),
]
_ORDER_ITEMS = [(1, 1, "x"), (2, 1, "y"), (3, 4, "x"), (4, 6, "y"), (5, 8, "x")]


def _sources() -> list[SemanticSource]:
    orders = SemanticSource(
        name="orders",
        connection="wh",
        table="orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="int", nullable=False),
            Column(name="order_date", type="date", nullable=False),
            Column(name="customer_id", type="int", nullable=False),
            Column(name="category", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
        ],
        measures=[
            Measure(name="revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="order_count", expr="count(order_id)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="order_day", column="order_date", granularity="day"),
            Dimension(name="order_month", column="order_date", granularity="month"),
            Dimension(name="order_year", column="order_date", granularity="year"),
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


def _binding(metric: str, **canonical: Any) -> MetricBinding:
    return MetricBinding(metric=metric, canonical=CanonicalRef(**canonical))


def _offset(metric: str, base: str, offset: str) -> MetricBinding:
    return _binding(metric, kind="offset", base=base, time_dimension="order_day", offset=offset)


def _resolver() -> ContractResolver:
    return ContractResolver(
        bindings=[
            _binding("revenue", source="orders", measure="revenue"),
            _binding("order_count", source="orders", measure="order_count"),
            _binding(
                "customers", kind="distinct_count", source="orders", distinct_on="customer_id"
            ),
            _binding(
                "revenue_from_2025",
                source="orders",
                measure="revenue",
                population_filter="order_date >= '2025-01-01'",
            ),
            _binding("aov", kind="ratio", numerator="revenue", denominator="order_count"),
            _binding(
                "revenue_per_customer", kind="ratio", numerator="revenue", denominator="customers"
            ),
            _binding(
                "revenue_to_date",
                kind="cumulative",
                source="orders",
                measure="revenue",
                order_by=["order_month"],
            ),
            _offset("revenue_py", "revenue", "1 year"),
            _offset("revenue_pm", "revenue", "1 month"),
            _offset("order_count_py", "order_count", "1 year"),
            _offset("customers_pm", "customers", "1 month"),
            _offset("aov_py", "aov", "1 year"),
            _offset("revenue_from_2025_py", "revenue_from_2025", "1 year"),
            _binding(
                "revenue_yoy",
                kind="derived",
                expr="(revenue - py) / py",
                inputs=[{"metric": "revenue"}, {"metric": "revenue_py", "alias": "py"}],
            ),
            _binding(
                "revenue_yoy_zero",
                kind="derived",
                expr="(revenue - py) / py",
                inputs=[{"metric": "revenue"}, {"metric": "revenue_py", "alias": "py"}],
                on_zero_denominator="zero",
            ),
            _binding(
                "ratio_of_ratios",
                kind="derived",
                expr="aov / revenue_per_customer",
                inputs=[{"metric": "aov"}, {"metric": "revenue_per_customer"}],
            ),
            _binding(
                "share_of_running_total",
                kind="derived",
                expr="ROUND(100 * revenue / revenue_to_date, 2)",
                inputs=[{"metric": "revenue"}, {"metric": "revenue_to_date"}],
            ),
            _binding(
                "growth_of_growth",
                kind="derived",
                expr="revenue_yoy - COALESCE(revenue_pm, 0)",
                inputs=[{"metric": "revenue_yoy"}, {"metric": "revenue_pm"}],
            ),
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
        return round(float(value), 4)
    return value


def _duckdb() -> tuple[Any, str]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (order_id INT, order_date DATE, customer_id INT, category TEXT, "
        "amount DECIMAL(10, 2))"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", _ORDERS)
    con.execute("CREATE TABLE order_items (item_id INT, order_id INT, sku TEXT)")
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?)", _ORDER_ITEMS)
    return con, "duckdb"


def _sqlite() -> tuple[Any, str]:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE orders (order_id INTEGER, order_date TEXT, customer_id INTEGER, "
        "category TEXT, amount REAL)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", _ORDERS)
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
        return result, sorted(rows, key=lambda r: tuple((v is None, str(v)) for v in r.values()))

    return run


@pytest.fixture(params=["duckdb", "sqlite"])
def run(request: pytest.FixtureRequest) -> Callable[..., tuple[CompileResult, list[Row]]]:
    con, dialect = _duckdb() if request.param == "duckdb" else _sqlite()
    return _runner(con, dialect)


def _by(rows: list[Row], key: str, value: str) -> dict[Any, Any]:
    return {row[key]: row[value] for row in rows if row[value] is not None}


def _shift_keys(values: dict[str, Any], offset: str) -> dict[str, Any]:
    duration = Duration.parse(offset)
    return {add_to_date(date.fromisoformat(k), duration).isoformat(): v for k, v in values.items()}


class TestOffset:
    def test_p1_last_year_by_month_is_revenue_one_year_earlier(self, run: Any) -> None:
        _, plain = run(["revenue"], ["order_month"])
        _, rows = run(["revenue_py"], ["order_month"])
        assert _by(rows, "order_month", "revenue_py") == _shift_keys(
            _by(plain, "order_month", "revenue"), "1 year"
        )

    def test_p13_a_month_offset_clamps_to_the_end_of_the_month(self, run: Any) -> None:
        _, rows = run(["revenue_pm"], ["order_day"])
        days = _by(rows, "order_day", "revenue_pm")
        assert days["2025-02-28"] == 40.0  # 2025-01-31 plus one month
        assert days["2025-01-31"] == 20.0  # 2024-12-31 plus one month

    def test_p2_a_year_offset_by_year(self, run: Any) -> None:
        _, plain = run(["revenue"], ["order_year"])
        _, rows = run(["revenue_py"], ["order_year"])
        assert _by(rows, "order_year", "revenue_py") == _shift_keys(
            _by(plain, "order_year", "revenue"), "1 year"
        )

    def test_p3_a_filter_on_the_time_column_is_read_in_output_time(self, run: Any) -> None:
        result, rows = run(["revenue_py"], ["order_month"], ["order_day >= '2026-01-01'"])
        _, plain = run(["revenue"], ["order_month"], ["order_day >= '2025-01-01'"])
        assert _by(rows, "order_month", "revenue_py") == _shift_keys(
            _by(plain, "order_month", "revenue"), "1 year"
        )
        offset = QueryMetadata.from_compile_result(result).offset["revenue_py"]
        assert offset.model_dump() == {
            "base": "revenue",
            "time_dimension": "order_day",
            "offset": "1 year",
            "shifted_filters": ["order_day >= '2026-01-01'"],
        }

    def test_p4_a_time_range_also_bounds_the_stored_column(self, run: Any) -> None:
        result, rows = run(
            ["revenue_py"],
            ["order_month"],
            time_range={"dimension": "order_day", "start": "2026-01-01", "end": "2026-03-01"},
        )
        _, filtered = run(
            ["revenue_py"],
            ["order_month"],
            ["order_day >= '2026-01-01'", "order_day < '2026-03-01'"],
        )
        assert rows == filtered
        assert _by(rows, "order_month", "revenue_py") == {"2026-01-01": 45.0, "2026-02-01": 8.0}
        assert "2024-12-29" in result.sql and "2025-03-04" in result.sql

    def test_p5_an_offset_ratio_divides_the_two_offsets(self, run: Any) -> None:
        _, rows = run(["aov_py", "revenue_py", "order_count_py"], ["order_month"])
        for row in rows:
            if row["order_count_py"]:
                assert row["aov_py"] == round(row["revenue_py"] / row["order_count_py"], 4)

    def test_p6_an_offset_distinct_count_counts_the_shifted_rows(self, run: Any) -> None:
        _, plain = run(["customers"], ["order_month"])
        _, rows = run(["customers_pm"], ["order_month"])
        assert _by(rows, "order_month", "customers_pm") == _shift_keys(
            _by(plain, "order_month", "customers"), "1 month"
        )
        # From 2025-02-01 in output time are the stored rows from 2025-01-01 on: customers 7, 8
        # and 9, three distinct ones, although the monthly counts add up to more.
        _, total = run(["customers_pm"], [], ["order_day >= '2025-02-01'"])
        assert total[0]["customers_pm"] == 3
        _, monthly = run(["customers_pm"], ["order_month"], ["order_day >= '2025-02-01'"])
        assert sum(r["customers_pm"] for r in monthly) > 3

    def test_p12_a_population_filter_reads_the_stored_date(self, run: Any) -> None:
        _, rows = run(["revenue_from_2025_py"], ["order_month"])
        values = _by(rows, "order_month", "revenue_from_2025_py")
        assert "2025-12-01" not in values  # 2024-12-31 is before the population starts
        assert values["2026-01-01"] == 45.0  # the 2025 January orders

    def test_without_a_time_dimension_or_filter_it_warns(self, run: Any) -> None:
        result, rows = run(["revenue_py"])
        assert rows == [{"revenue_py": 93.0}]
        assert any("has no effect here" in w for w in result.warnings)

    def test_p11_a_fanning_dimension_matches_the_base_alone(self, run: Any) -> None:
        _, plain = run(["revenue"], ["order_month", "sku"])
        _, rows = run(["revenue_py"], ["order_month", "sku"])
        assert {(r["order_month"], r["sku"]): r["revenue_py"] for r in rows} == {
            (
                add_to_date(date.fromisoformat(r["order_month"]), Duration.parse("1y")).isoformat(),
                r["sku"],
            ): r["revenue"]
            for r in plain
        }


class TestDerived:
    def test_p7_growth_against_last_year(self, run: Any) -> None:
        _, rows = run(["revenue", "revenue_py", "revenue_yoy", "revenue_yoy_zero"], ["order_month"])
        for row in rows:
            if row["revenue"] is not None and row["revenue_py"]:
                expected = round((row["revenue"] - row["revenue_py"]) / row["revenue_py"], 4)
                assert row["revenue_yoy"] == expected
            else:
                assert row["revenue_yoy"] is None
        assert any(r["revenue_yoy"] is None for r in rows)

    def test_p9_a_ratio_of_two_ratios(self, run: Any) -> None:
        _, rows = run(["ratio_of_ratios", "aov", "revenue_per_customer"], ["order_month"])
        for row in rows:
            assert row["ratio_of_ratios"] == pytest.approx(
                row["aov"] / row["revenue_per_customer"], abs=1e-4
            )

    def test_p8_a_running_total_input_is_used_after_accumulation(self, run: Any) -> None:
        _, rows = run(["share_of_running_total", "revenue", "revenue_to_date"], ["order_month"])
        for row in rows:
            if row["order_month"] is None:
                continue
            assert row["share_of_running_total"] == round(
                100 * row["revenue"] / row["revenue_to_date"], 2
            )

    def test_a_derived_metric_over_a_derived_metric(self, run: Any) -> None:
        _, rows = run(["growth_of_growth", "revenue_yoy", "revenue_pm"], ["order_month"])
        for row in rows:
            if row["revenue_yoy"] is not None:
                assert row["growth_of_growth"] == pytest.approx(
                    row["revenue_yoy"] - (row["revenue_pm"] or 0), abs=1e-4
                )

    def test_p10_shifted_and_unshifted_leaves_never_fuse(self, run: Any) -> None:
        _, fused = run(["revenue", "revenue_py", "revenue_yoy"], ["order_month"])
        _, apart = run(["revenue", "revenue_py", "revenue_yoy"], ["order_month"], dedup=False)
        assert fused == apart
        assert {r["order_month"] for r in fused if r["revenue_py"] is not None} != {
            r["order_month"] for r in fused if r["revenue"] is not None
        }

    def test_metadata_names_the_inputs(self, run: Any) -> None:
        result, _ = run(["revenue_yoy"], ["order_month"])
        metadata = QueryMetadata.from_compile_result(result)
        assert metadata.derived["revenue_yoy"].model_dump() == {
            "expr": "(revenue - py) / py",
            "inputs": [
                {"alias": "revenue", "metric": "revenue", "resolved": "orders.revenue"},
                {
                    "alias": "py",
                    "metric": "revenue_py",
                    "resolved": "offset(orders.revenue, order_day, 1 year)",
                },
            ],
            "on_zero_denominator": "null",
        }
        assert set(metadata.offset) == {"revenue_py"}
        assert result.resolved["revenue_yoy"] == "derived((revenue - py) / py)"
