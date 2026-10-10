"""Cumulative metrics: running totals along fixed order dimensions.

Every test here executes the compiled SQL against real data rather than asserting on SQL
shape, on DuckDB and, where the feature is dialect-neutral, on SQLite too. The oracle is the
plain ``revenue`` metric queried at the same grain and summed up in Python, so a running
total is checked against the number it has to equal, not against a second copy of the
compiler's own logic.

The data has a gap (category ``b`` has no order on 2025-01-03), a partition that starts
later than the other (``b`` starts on 2025-01-02), and an order without a date, which has to
sort last on every engine.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
)
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import QueryMetadata
from canonic.exc import UnsupportedMeasure
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.compiler.result import CompileResult

Row = dict[str, Any]

_ORDERS = [
    (1, "2025-01-01", "a", "n", "10.00", "completed"),
    (2, "2025-01-02", "b", "n", "5.00", "completed"),
    (3, "2025-01-03", "a", "s", "7.00", "completed"),
    (4, "2025-01-05", "b", "s", "1.00", "completed"),
    (5, "2025-01-06", "a", "n", "2.00", "completed"),
    (6, "2025-01-06", "a", "s", "3.00", "refunded"),
    (7, "2025-02-10", "b", "n", "4.00", "completed"),
    (8, None, "a", "n", "100.00", "completed"),
]
_ORDER_ITEMS = [(1, 1, "x"), (2, 1, "y"), (3, 3, "x"), (4, 5, "y"), (5, 7, "x")]
_VISITS = [(1, "2025-01-01", "b", 3), (2, "2025-01-04", "a", 1)]


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
            Column(name="region", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[Measure(name="revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[
            Dimension(name="order_day", column="order_date", granularity="day"),
            Dimension(name="order_month", column="order_date", granularity="month"),
            Dimension(name="order_year", expr="year(order_date)", type="int"),
            Dimension(name="order_mon", expr="month(order_date)", type="int"),
            Dimension(name="category", column="category"),
            Dimension(name="region", column="region"),
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
    visits = SemanticSource(
        name="visits",
        connection="wh",
        table="visits",
        grain=["visit_id"],
        columns=[
            Column(name="visit_id", type="int", nullable=False),
            Column(name="visit_date", type="date", nullable=False),
            Column(name="category", type="string", nullable=False),
            Column(name="n", type="int", nullable=False),
        ],
        measures=[Measure(name="visit_count", expr="sum(n)", additivity="additive")],
        dimensions=[
            Dimension(name="order_day", column="visit_date", granularity="day"),
            Dimension(name="category", column="category"),
        ],
    )
    return [orders, order_items, visits]


def _cumulative(metric: str, order_by: list[str], **extra: Any) -> MetricBinding:
    return MetricBinding(
        metric=metric,
        canonical=CanonicalRef(
            kind="cumulative", source="orders", measure="revenue", order_by=order_by, **extra
        ),
    )


def _resolver(*, guardrails: tuple[Guardrail, ...] = ()) -> ContractResolver:
    return ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
            ),
            *(
                MetricBinding(
                    metric=f"revenue_{category}",
                    canonical=CanonicalRef(
                        source="orders",
                        measure="revenue",
                        population_filter=f"category = '{category}'",
                    ),
                )
                for category in ("a", "b")
            ),
            MetricBinding(
                metric="visit_count",
                canonical=CanonicalRef(source="visits", measure="visit_count"),
            ),
            _cumulative("cumulative_revenue", ["order_day"]),
            _cumulative("filled_revenue", ["order_day"], on_gap="fill_observed"),
            _cumulative("monthly_cumulative_revenue", ["order_month"]),
            _cumulative("year_month_cumulative_revenue", ["order_year", "order_mon"]),
            _cumulative(
                "revenue_since_jan_3",
                ["order_day"],
                population_filter="order_date >= '2025-01-03'",
            ),
            MetricBinding(
                metric="cumulative_share",
                canonical=CanonicalRef(
                    kind="ratio", numerator="cumulative_revenue", denominator="revenue"
                ),
            ),
        ],
        guardrails=guardrails,
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
        "CREATE TABLE orders (order_id INT, order_date DATE, category TEXT, region TEXT, "
        "amount DECIMAL(10, 2), status TEXT)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", _ORDERS)
    con.execute("CREATE TABLE order_items (item_id INT, order_id INT, sku TEXT)")
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?)", _ORDER_ITEMS)
    con.execute("CREATE TABLE visits (visit_id INT, visit_date DATE, category TEXT, n INT)")
    con.executemany("INSERT INTO visits VALUES (?, ?, ?, ?)", _VISITS)
    return con, "duckdb"


def _sqlite() -> tuple[Any, str]:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE orders (order_id INTEGER, order_date TEXT, category TEXT, region TEXT, "
        "amount REAL, status TEXT)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", _ORDERS)
    con.execute("CREATE TABLE order_items (item_id INTEGER, order_id INTEGER, sku TEXT)")
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?)", _ORDER_ITEMS)
    con.execute("CREATE TABLE visits (visit_id INTEGER, visit_date TEXT, category TEXT, n INTEGER)")
    con.executemany("INSERT INTO visits VALUES (?, ?, ?, ?)", _VISITS)
    return con, "sqlite"


def _runner(con: Any, dialect: str) -> Callable[..., tuple[CompileResult, list[Row]]]:
    def run(
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: list[str] | None = None,
        *,
        resolver: ContractResolver | None = None,
        dedup: bool = True,
    ) -> tuple[CompileResult, list[Row]]:
        result = compile(
            SemanticQuery(metrics=metrics, dimensions=dimensions or [], filters=filters or []),
            resolver or _resolver(),
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


@pytest.fixture
def run_duckdb() -> Callable[..., tuple[CompileResult, list[Row]]]:
    return _runner(*_duckdb())


def _running(
    rows: list[Row], value: str, order_by: list[str], partition_by: list[str]
) -> dict[tuple[Any, ...], float]:
    """Running totals of ``value`` per partition, ordered with NULLs last: the oracle."""
    totals: dict[tuple[Any, ...], float] = {}
    by_partition: dict[tuple[Any, ...], list[Row]] = {}
    for row in rows:
        by_partition.setdefault(tuple(row[p] for p in partition_by), []).append(row)
    for partition_rows in by_partition.values():
        ordered = sorted(
            partition_rows, key=lambda r: tuple((r[o] is None, r[o] or "") for o in order_by)
        )
        total = 0.0
        for row in ordered:
            total += row[value] or 0.0
            key = tuple(row[d] for d in [*order_by, *partition_by])
            totals[key] = round(total, 2)
    return totals


def _values(
    rows: list[Row], metric: str, order_by: list[str], partition_by: list[str]
) -> dict[tuple[Any, ...], Any]:
    return {tuple(row[d] for d in [*order_by, *partition_by]): row[metric] for row in rows}


class TestRunningTotal:
    def test_s1_each_row_is_the_sum_up_to_its_order_tuple(self, run: Any) -> None:
        _, cumulative = run(["cumulative_revenue"], ["order_day"])
        _, base = run(["revenue"], ["order_day"])
        assert _values(cumulative, "cumulative_revenue", ["order_day"], []) == _running(
            base, "revenue", ["order_day"], []
        )
        assert cumulative[-1] == {"order_day": None, "cumulative_revenue": 132.0}

    def test_s2_partitions_accumulate_independently(self, run: Any) -> None:
        dims = ["order_day", "category"]
        _, cumulative = run(["cumulative_revenue"], dims)
        _, base = run(["revenue"], dims)
        assert _values(cumulative, "cumulative_revenue", ["order_day"], ["category"]) == _running(
            base, "revenue", ["order_day"], ["category"]
        )

        _, overall = run(["cumulative_revenue"], ["order_day"])
        last_per_category = {row["category"]: row["cumulative_revenue"] for row in cumulative}
        assert sum(last_per_category.values()) == overall[-1]["cumulative_revenue"]

    def test_partition_by_two_dimensions(self, run: Any) -> None:
        dims = ["order_day", "category", "region"]
        _, cumulative = run(["cumulative_revenue"], dims)
        _, base = run(["revenue"], dims)
        assert _values(
            cumulative, "cumulative_revenue", ["order_day"], ["category", "region"]
        ) == _running(base, "revenue", ["order_day"], ["category", "region"])

    def test_s3_two_order_dimensions_match_a_month_start(self, run_duckdb: Any) -> None:
        _, by_year_month = run_duckdb(
            ["year_month_cumulative_revenue"], ["order_year", "order_mon"]
        )
        _, by_month = run_duckdb(["monthly_cumulative_revenue"], ["order_month"])
        assert [r["year_month_cumulative_revenue"] for r in by_year_month] == [
            r["monthly_cumulative_revenue"] for r in by_month
        ]
        assert [r["monthly_cumulative_revenue"] for r in by_month] == [28.0, 32.0, 132.0]

    def test_s3_order_is_lexicographic_not_per_dimension(self, run_duckdb: Any) -> None:
        _, rows = run_duckdb(["year_month_cumulative_revenue"], ["order_year", "order_mon"])
        assert [(r["order_year"], r["order_mon"]) for r in rows] == [
            (2025, 1),
            (2025, 2),
            (None, None),
        ]

    def test_s4_missing_order_dimension_is_named(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="'order_day'"):
            run(["cumulative_revenue"], ["category"])

    def test_s4_scalar_query_is_refused(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="'order_day'"):
            run(["cumulative_revenue"])

    def test_s11_fanning_join_matches_the_single_metric(self, run_duckdb: Any) -> None:
        dims = ["order_day", "sku"]
        _, cumulative = run_duckdb(["cumulative_revenue"], dims)
        _, base = run_duckdb(["revenue"], dims)
        assert _values(cumulative, "cumulative_revenue", ["order_day"], ["sku"]) == _running(
            base, "revenue", ["order_day"], ["sku"]
        )

    def test_s12_nulls_sort_last_on_every_dialect(self) -> None:
        results = []
        for con, dialect in (_duckdb(), _sqlite()):
            run = _runner(con, dialect)
            for query in (
                (["cumulative_revenue"], ["order_day"]),
                (["cumulative_revenue"], ["order_day", "category"]),
                (["filled_revenue"], ["order_day", "category"]),
                (["monthly_cumulative_revenue"], ["order_month"], ["order_date >= '2025-01-04'"]),
            ):
                results.append((dialect, query, run(*query)[1]))
        duck = [rows for dialect, _, rows in results if dialect == "duckdb"]
        lite = [rows for dialect, _, rows in results if dialect == "sqlite"]
        assert duck == lite
        null_day = [r for r in duck[1] if r["order_day"] is None]
        assert null_day == [{"order_day": None, "category": "a", "cumulative_revenue": 122.0}]

    def test_leaf_deduplication_does_not_change_numbers(self, run: Any) -> None:
        metrics = ["cumulative_revenue", "filled_revenue", "revenue", "visit_count"]
        dims = ["order_day", "category"]
        filters = ["order_day >= '2025-01-03'"]
        assert run(metrics, dims, filters)[1] == run(metrics, dims, filters, dedup=False)[1]


class TestFilters:
    def test_s5_order_filter_shows_tuples_without_restarting(self, run: Any) -> None:
        dims = ["order_day", "category"]
        _, unfiltered = run(["cumulative_revenue"], dims)
        result, filtered = run(["cumulative_revenue"], dims, ["order_day >= '2025-01-05'"])
        assert filtered == [
            r for r in unfiltered if r["order_day"] is not None and r["order_day"] >= "2025-01-05"
        ]
        assert filtered[0] == {
            "order_day": "2025-01-05",
            "category": "b",
            "cumulative_revenue": 6.0,
        }
        assert result.cumulative is not None
        assert result.cumulative.visibility_filters == ["order_day >= '2025-01-05'"]

    def test_s5_filter_on_the_raw_order_column(self, run: Any) -> None:
        _, by_dimension = run(["cumulative_revenue"], ["order_day"], ["order_day >= '2025-01-05'"])
        _, by_column = run(["cumulative_revenue"], ["order_day"], ["order_date >= '2025-01-05'"])
        assert by_dimension == by_column

    def test_coarse_bucket_is_shown_when_one_of_its_rows_matches(self, run: Any) -> None:
        _, rows = run(
            ["monthly_cumulative_revenue"], ["order_month"], ["order_date >= '2025-01-04'"]
        )
        assert [r["monthly_cumulative_revenue"] for r in rows] == [28.0, 32.0]

    @pytest.mark.parametrize("connect", [_duckdb, _sqlite])
    def test_order_filter_keeps_metrics_sharing_a_measure_apart(
        self, connect: Callable[[], tuple[Any, str]]
    ) -> None:
        # revenue_a and revenue_b both come out under the measure's name, so the rows are read
        # by position: a lookup by name in the accumulated CTE once returned revenue_a twice.
        con, dialect = connect()
        result = compile(
            SemanticQuery(
                metrics=["revenue_a", "revenue_b", "cumulative_revenue"],
                dimensions=["order_day"],
                filters=["order_day >= '2025-01-05'"],
            ),
            _resolver(),
            _sources(),
            connection_dialects={"wh": dialect},
        )
        cursor = con.execute(result.sql)
        assert [d[0] for d in cursor.description] == [
            "order_day",
            "revenue",
            "revenue",
            "cumulative_revenue",
        ]
        rows = sorted(tuple(_normalize(v) for v in row) for row in cursor.fetchall())
        assert rows == [
            ("2025-01-05", None, 1.0, 23.0),
            ("2025-01-06", 5.0, None, 28.0),
            ("2025-02-10", None, 4.0, 32.0),
        ]

    def test_s6_partition_filter_keeps_values(self, run: Any) -> None:
        dims = ["order_day", "category"]
        _, unfiltered = run(["cumulative_revenue"], dims)
        result, filtered = run(["cumulative_revenue"], dims, ["category = 'a'"])
        assert filtered == [r for r in unfiltered if r["category"] == "a"]
        assert result.cumulative is not None
        assert result.cumulative.visibility_filters == []

    def test_s7_filter_on_order_and_partition_columns_is_refused(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="reads order and non-order columns"):
            run(
                ["cumulative_revenue"],
                ["order_day", "category"],
                ["order_day >= '2025-01-03' OR category = 'a'"],
            )

    def test_population_filter_restricts_what_is_accumulated(self, run: Any) -> None:
        _, rows = run(["revenue_since_jan_3"], ["order_day", "category"])
        first_a = next(r for r in rows if r["category"] == "a")
        assert first_a == {"order_day": "2025-01-03", "category": "a", "revenue_since_jan_3": 7.0}

    def test_mandatory_filter_guardrail_applies_before_accumulating(self, run: Any) -> None:
        guardrail = Guardrail(
            id="completed-only",
            applies_to=AppliesTo(source="orders"),
            kind=GuardrailKind.MANDATORY_FILTER,
            filter="status = 'completed'",
            rationale="refunds are not revenue",
        )
        result, rows = run(
            ["cumulative_revenue"],
            ["order_day", "category"],
            resolver=_resolver(guardrails=(guardrail,)),
        )
        assert [g.id for g in result.guardrails_fired] == ["completed-only"]
        jan_6 = next(r for r in rows if r["order_day"] == "2025-01-06")
        assert jan_6["cumulative_revenue"] == 19.0


class TestGaps:
    def test_s8_skip_leaves_the_gap_out(self, run: Any) -> None:
        _, rows = run(["cumulative_revenue"], ["order_day", "category"])
        b = [(r["order_day"], r["cumulative_revenue"]) for r in rows if r["category"] == "b"]
        assert b == [("2025-01-02", 5.0), ("2025-01-05", 6.0), ("2025-02-10", 10.0)]

    def test_s9_fill_observed_carries_the_total(self, run: Any) -> None:
        result, rows = run(["filled_revenue"], ["order_day", "category"])
        b = [(r["order_day"], r["filled_revenue"]) for r in rows if r["category"] == "b"]
        assert b == [
            ("2025-01-02", 5.0),
            ("2025-01-03", 5.0),
            ("2025-01-05", 6.0),
            ("2025-01-06", 6.0),
            ("2025-02-10", 10.0),
            (None, 10.0),
        ]
        assert any("fill_observed" in w for w in result.warnings)

    def test_s9_fill_observed_adds_no_leading_rows(self, run: Any) -> None:
        _, rows = run(["filled_revenue"], ["order_day", "category"])
        assert not [r for r in rows if r["category"] == "b" and r["order_day"] == "2025-01-01"]

    def test_fill_observed_without_partition_equals_skip(self, run: Any) -> None:
        result, filled = run(["filled_revenue"], ["order_day"])
        _, skipped = run(["cumulative_revenue"], ["order_day"])
        assert [r["filled_revenue"] for r in filled] == [r["cumulative_revenue"] for r in skipped]
        assert result.warnings == []

    def test_fill_observed_rows_respect_visibility(self, run: Any) -> None:
        _, rows = run(["filled_revenue"], ["order_day", "category"], ["order_day >= '2025-01-06'"])
        assert rows == [
            {"order_day": "2025-01-06", "category": "a", "filled_revenue": 22.0},
            {"order_day": "2025-01-06", "category": "b", "filled_revenue": 6.0},
            {"order_day": "2025-02-10", "category": "a", "filled_revenue": 22.0},
            {"order_day": "2025-02-10", "category": "b", "filled_revenue": 10.0},
        ]


class TestComposition:
    def test_s10_sibling_rows_carry_the_total_and_lead_with_null(self, run: Any) -> None:
        _, rows = run(["cumulative_revenue", "visit_count"], ["order_day", "category"])
        by_key = {(r["order_day"], r["category"]): r for r in rows}
        assert by_key[("2025-01-01", "b")] == {
            "order_day": "2025-01-01",
            "category": "b",
            "cumulative_revenue": None,
            "visit_count": 3,
        }
        assert by_key[("2025-01-04", "a")]["cumulative_revenue"] == 17.0
        assert by_key[("2025-01-04", "a")]["visit_count"] == 1

    def test_non_cumulative_sibling_keeps_its_own_filter(self, run: Any) -> None:
        _, rows = run(
            ["cumulative_revenue", "revenue"],
            ["order_day", "category"],
            ["order_day >= '2025-01-06'"],
        )
        assert rows == [
            {
                "order_day": "2025-01-06",
                "category": "a",
                "cumulative_revenue": 22.0,
                "revenue": 5.0,
            },
            {
                "order_day": "2025-02-10",
                "category": "b",
                "cumulative_revenue": 10.0,
                "revenue": 4.0,
            },
        ]

    def test_s13_cumulative_ratio_component_is_refused(self, run: Any) -> None:
        with pytest.raises(UnsupportedMeasure, match="'cumulative_revenue' has kind 'cumulative'"):
            run(["cumulative_share"], ["order_day"])


class TestMetadata:
    def test_compile_result_and_wire_metadata(self, run: Any) -> None:
        result, _ = run(
            ["cumulative_revenue"], ["order_day", "category"], ["order_day >= '2025-01-05'"]
        )
        assert result.resolved == {"cumulative_revenue": "cumulative(orders.revenue)"}
        metadata = QueryMetadata.from_compile_result(result).cumulative
        assert metadata is not None
        assert metadata.model_dump() == {
            "metric": "cumulative_revenue",
            "order_by": ["order_day"],
            "partition_by": ["category"],
            "on_gap": "skip",
            "visibility_filters": ["order_day >= '2025-01-05'"],
        }

    def test_no_cumulative_metadata_for_other_kinds(self, run: Any) -> None:
        result, _ = run(["revenue"], ["order_day"])
        assert result.cumulative is None
        assert QueryMetadata.from_compile_result(result).cumulative is None
