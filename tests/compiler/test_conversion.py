"""Conversion metrics: conversion events attributed to the latest earlier base event.

Every test executes the compiled SQL on DuckDB and SQLite. The oracle matches the events in
Python, conversion by conversion, so the compiler is checked against the attribution rule
itself and not against a second copy of its SQL.

The visits and purchases include a purchase at exactly the end of the window, one just past it,
one before the user's first visit, one of an unknown user, two visits of one user at the same
instant, a user who never buys, and a page view join that fans out.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.compiler.compose import zero_denominator_column, zero_denominator_warning
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import QueryMetadata
from canonic.exc import FanoutUnsafe, UnsupportedMeasure
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.compiler.result import CompileResult

Row = dict[str, Any]

_USERS = [(1, "de"), (2, "fr"), (3, "de"), (4, "fr"), (5, "de")]
_VISITS = [
    (1, 1, "2024-01-01 09:00:00", "ads", "x"),
    (2, 1, "2024-01-03 12:00:00", "seo", "y"),
    (3, 2, "2024-01-02 08:00:00", "ads", "x"),
    (4, 3, "2024-01-10 10:00:00", "seo", "x"),
    (5, 3, "2024-01-10 10:00:00", "ads", "x"),
    (6, 4, "2024-02-01 00:00:00", "mail", "z"),
    (7, 2, "2024-02-05 09:00:00", "mail", "y"),
    (8, 5, "2024-02-07 09:00:00", "social", "x"),
]
_PURCHASES = [
    (1, 1, "2024-01-05 09:00:00", "y", "10.00", "settled"),
    (2, 1, "2024-01-10 12:00:00", "x", "20.00", "settled"),
    (3, 2, "2024-01-20 08:00:00", "x", "5.00", "refunded"),
    (4, 3, "2024-01-10 10:30:00", "x", "7.00", "settled"),
    (5, 4, "2024-01-15 00:00:00", "z", "3.00", "settled"),
    (6, 2, "2024-02-06 09:00:00", "x", "8.00", "settled"),
    (7, 9, "2024-01-02 00:00:00", "x", "1.00", "settled"),
]
_PAGE_VIEWS = [(1, 1), (2, 1), (3, 2)]


def _sources() -> list[SemanticSource]:
    visits = SemanticSource(
        name="visits",
        connection="wh",
        table="visits",
        grain=["visit_id"],
        columns=[
            Column(name="visit_id", type="int", nullable=False),
            Column(name="user_id", type="int", nullable=False),
            Column(name="visited_at", type="timestamp", nullable=False),
            Column(name="channel", type="string", nullable=False),
            Column(name="product", type="string", nullable=False),
        ],
        measures=[Measure(name="visits", expr="count(visit_id)", additivity="additive")],
        dimensions=[
            Dimension(name="channel", column="channel"),
            Dimension(name="visit_month", column="visited_at", granularity="month"),
            Dimension(name="visited_at", column="visited_at"),
        ],
        joins=[
            Join(
                to="users",
                on="visits.user_id = users.user_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="page_views",
                on="visits.visit_id = page_views.visit_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )
    users = SemanticSource(
        name="users",
        connection="wh",
        table="users",
        grain=["user_id"],
        columns=[
            Column(name="user_id", type="int", nullable=False),
            Column(name="country", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="country", column="country")],
    )
    page_views = SemanticSource(
        name="page_views",
        connection="wh",
        table="page_views",
        grain=["view_id"],
        columns=[
            Column(name="view_id", type="int", nullable=False),
            Column(name="visit_id", type="int", nullable=False),
        ],
        dimensions=[Dimension(name="view_id", column="view_id")],
    )
    purchases = SemanticSource(
        name="purchases",
        connection="wh",
        table="purchases",
        grain=["purchase_id"],
        columns=[
            Column(name="purchase_id", type="int", nullable=False),
            Column(name="user_id", type="int", nullable=False),
            Column(name="purchased_at", type="timestamp", nullable=False),
            Column(name="sku", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[
            Measure(name="purchases", expr="count(purchase_id)", additivity="additive"),
            Measure(name="purchase_revenue", expr="sum(amount)", additivity="additive"),
        ],
        dimensions=[Dimension(name="purchase_product", column="sku")],
    )
    return [visits, users, page_views, purchases]


def _binding(metric: str, **canonical: Any) -> MetricBinding:
    return MetricBinding(metric=metric, canonical=CanonicalRef(**canonical))


def _conversion(metric: str, **overrides: Any) -> MetricBinding:
    fields: dict[str, Any] = {
        "kind": "conversion",
        "base_metric": "visits",
        "conversion_metric": "purchases",
        "entity": "user_id",
        "base_time": "visited_at",
        "conversion_time": "purchased_at",
        "window": "7 days",
    }
    fields.update(overrides)
    return _binding(metric, **{k: v for k, v in fields.items() if v is not None})


def _resolver() -> ContractResolver:
    return ContractResolver(
        bindings=[
            _binding("visits", source="visits", measure="visits"),
            _binding("visitors", kind="distinct_count", source="visits", distinct_on="user_id"),
            _binding("purchases", source="purchases", measure="purchases"),
            _binding("purchase_revenue", source="purchases", measure="purchase_revenue"),
            _binding("buyers", kind="distinct_count", source="purchases", distinct_on="user_id"),
            _binding(
                "settled_purchases",
                source="purchases",
                measure="purchases",
                population_filter="status = 'settled'",
            ),
            _conversion("rate_7d"),
            _conversion("conversions_7d", calculation="conversions"),
            _conversion("rate_ever", window=None),
            _conversion("rate_48h", window="48 hours"),
            _conversion(
                "rate_same_product",
                window=None,
                constant_properties=[{"base": "product", "conversion": "sku"}],
            ),
            _conversion(
                "revenue_7d", conversion_metric="purchase_revenue", calculation="conversions"
            ),
            _conversion("buyer_rate_7d", base_metric="visitors", conversion_metric="buyers"),
            _conversion("settled_rate_7d", conversion_metric="settled_purchases"),
            _conversion("rate_7d_zero", on_zero_denominator="zero"),
            _binding(
                "rate_7d_percent",
                kind="derived",
                expr="ROUND(100 * rate_7d, 2)",
                inputs=[{"metric": "rate_7d"}],
            ),
        ],
        guardrails=(),
        finality=(),
        assertions=(),
    )


@dataclass(frozen=True, slots=True)
class _Visit:
    visit_id: int
    user_id: int
    at: datetime
    channel: str
    product: str

    @property
    def country(self) -> str:
        return dict(_USERS)[self.user_id]


@dataclass(frozen=True, slots=True)
class _Purchase:
    purchase_id: int
    user_id: int
    at: datetime
    sku: str
    amount: float
    status: str


def _visits() -> list[_Visit]:
    return [_Visit(v, u, datetime.fromisoformat(t), c, p) for v, u, t, c, p in _VISITS]


def _purchases() -> list[_Purchase]:
    return [
        _Purchase(p, u, datetime.fromisoformat(t), s, float(a), st)
        for p, u, t, s, a, st in _PURCHASES
    ]


def _attributed(
    window: timedelta | None,
    *,
    visits: list[_Visit] | None = None,
    same_product: bool = False,
    by: Callable[[_Visit], Any] = lambda v: v.channel,
) -> dict[int, _Visit]:
    """Each purchase's base visit: the latest one of its user at or before it, in the window."""
    candidates = visits if visits is not None else _visits()
    chosen: dict[int, _Visit] = {}
    for purchase in _purchases():
        matches = [
            v
            for v in candidates
            if v.user_id == purchase.user_id
            and v.at <= purchase.at
            and (window is None or purchase.at <= v.at + window)
            and (not same_product or v.product == purchase.sku)
        ]
        if matches:
            latest = max(v.at for v in matches)
            chosen[purchase.purchase_id] = min((v for v in matches if v.at == latest), key=by)
    return chosen


def _oracle(
    window: timedelta | None,
    *,
    by: Callable[[_Visit], Any] = lambda v: v.channel,
    visits: list[_Visit] | None = None,
    same_product: bool = False,
    counted: Callable[[_Purchase], bool] = lambda p: True,
    value: Callable[[_Purchase], float] = lambda p: 1.0,
) -> dict[Any, float]:
    """The conversions per group of the base visit, ``0`` for a group with visits only."""
    base = visits if visits is not None else _visits()
    totals: dict[Any, float] = {by(v): 0.0 for v in base}
    purchases = {p.purchase_id: p for p in _purchases() if counted(p)}
    for purchase_id, visit in _attributed(
        window, visits=base, same_product=same_product, by=by
    ).items():
        if purchase_id in purchases:
            totals[by(visit)] += value(purchases[purchase_id])
    return totals


def _rate(conversions: dict[Any, float], by: Callable[[_Visit], Any] = lambda v: v.channel):
    visits: dict[Any, int] = defaultdict(int)
    for v in _visits():
        visits[by(v)] += 1
    return {k: round(c / visits[k], 4) for k, c in conversions.items()}


def _normalize(value: Any) -> Any:
    if isinstance(value, (Decimal, float)):
        return round(float(value), 4)
    return value


def _duckdb() -> tuple[Any, str]:
    con = duckdb.connect()
    con.execute("CREATE TABLE users (user_id INT, country TEXT)")
    con.executemany("INSERT INTO users VALUES (?, ?)", _USERS)
    con.execute(
        "CREATE TABLE visits (visit_id INT, user_id INT, visited_at TIMESTAMP, channel TEXT, "
        "product TEXT)"
    )
    con.executemany("INSERT INTO visits VALUES (?, ?, ?, ?, ?)", _VISITS)
    con.execute("CREATE TABLE page_views (view_id INT, visit_id INT)")
    con.executemany("INSERT INTO page_views VALUES (?, ?)", _PAGE_VIEWS)
    con.execute(
        "CREATE TABLE purchases (purchase_id INT, user_id INT, purchased_at TIMESTAMP, sku TEXT, "
        "amount DECIMAL(10, 2), status TEXT)"
    )
    con.executemany("INSERT INTO purchases VALUES (?, ?, ?, ?, ?, ?)", _PURCHASES)
    return con, "duckdb"


def _sqlite() -> tuple[Any, str]:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE users (user_id INTEGER, country TEXT)")
    con.executemany("INSERT INTO users VALUES (?, ?)", _USERS)
    con.execute(
        "CREATE TABLE visits (visit_id INTEGER, user_id INTEGER, visited_at TEXT, channel TEXT, "
        "product TEXT)"
    )
    con.executemany("INSERT INTO visits VALUES (?, ?, ?, ?, ?)", _VISITS)
    con.execute("CREATE TABLE page_views (view_id INTEGER, visit_id INTEGER)")
    con.executemany("INSERT INTO page_views VALUES (?, ?)", _PAGE_VIEWS)
    con.execute(
        "CREATE TABLE purchases (purchase_id INTEGER, user_id INTEGER, purchased_at TEXT, "
        "sku TEXT, amount REAL, status TEXT)"
    )
    con.executemany("INSERT INTO purchases VALUES (?, ?, ?, ?, ?, ?)", _PURCHASES)
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
    return {row[key]: row[value] for row in rows}


_WEEK = timedelta(days=7)


class TestMatching:
    def test_c1_the_window_includes_its_end(self, run: Any) -> None:
        _, rows = run(["conversions_7d"], ["channel"])
        assert _by(rows, "channel", "conversions_7d") == _oracle(_WEEK)
        # Purchase 2 is exactly seven days after visit 2 and counts for seo.
        assert _by(rows, "channel", "conversions_7d")["seo"] == 2

    def test_c2_without_a_window_every_later_purchase_counts(self, run: Any) -> None:
        _, rows = run(["rate_ever"], ["channel"])
        assert _by(rows, "channel", "rate_ever") == _rate(_oracle(None))

    def test_c3_the_latest_visit_wins_and_a_tie_goes_to_the_first_channel(self, run: Any) -> None:
        attributed = _attributed(_WEEK)
        assert attributed[1].channel == "seo"  # visits 1 and 2 match, visit 2 is later
        assert attributed[4].channel == "ads"  # visits 4 and 5 are at the same instant
        _, rows = run(["conversions_7d"], ["channel"])
        assert _by(rows, "channel", "conversions_7d") == {
            "ads": 1,
            "mail": 1,
            "seo": 2,
            "social": 0,
        }

    def test_purchases_before_a_visit_or_of_unknown_users_never_count(self, run: Any) -> None:
        _, rows = run(["conversions_7d", "rate_ever"])
        # Purchase 5 precedes user 4's only visit and purchase 7 belongs to no visitor.
        assert rows == [
            {
                "conversions_7d": 4,
                "rate_ever": round(5 / 8, 4),
                zero_denominator_column("rate_ever"): 0,
            }
        ]

    def test_c6_a_constant_property_restricts_the_matches(self, run: Any) -> None:
        _, rows = run(["rate_same_product"], ["channel"])
        assert _by(rows, "channel", "rate_same_product") == _rate(_oracle(None, same_product=True))

    def test_c11_a_window_in_hours_compares_timestamps(self, run: Any) -> None:
        _, rows = run(["rate_48h"], ["channel"])
        assert _by(rows, "channel", "rate_48h") == _rate(_oracle(timedelta(hours=48)))
        # Purchase 1 counts for visit 2 after 45 hours, purchase 2 after a week does not.
        assert _by(rows, "channel", "rate_48h")["seo"] == 0.5


class TestValues:
    def test_c4_the_rate_divides_by_the_base_metric_queried_alone(self, run: Any) -> None:
        _, rows = run(["rate_7d", "conversions_7d", "visits"], ["channel"])
        for row in rows:
            assert row["rate_7d"] == round(row["conversions_7d"] / row["visits"], 4)
        assert _by(rows, "channel", "rate_7d")["social"] == 0

    def test_a_sum_counts_the_attributed_purchase_amounts(self, run: Any) -> None:
        _, rows = run(["revenue_7d"], ["channel"])
        assert _by(rows, "channel", "revenue_7d") == _oracle(_WEEK, value=lambda p: p.amount)

    def test_distinct_counts_on_both_sides(self, run: Any) -> None:
        _, rows = run(["buyer_rate_7d"], ["channel"])
        buyers: dict[str, set[int]] = defaultdict(set)
        purchases = {p.purchase_id: p for p in _purchases()}
        for purchase_id, visit in _attributed(_WEEK).items():
            buyers[visit.channel].add(purchases[purchase_id].user_id)
        visitors: dict[str, set[int]] = defaultdict(set)
        for v in _visits():
            visitors[v.channel].add(v.user_id)
        assert _by(rows, "channel", "buyer_rate_7d") == {
            channel: round(len(buyers[channel]) / len(users), 4)
            for channel, users in visitors.items()
        }

    def test_c9_the_conversion_metric_keeps_its_own_population(self, run: Any) -> None:
        _, rows = run(["settled_rate_7d"], ["channel"])
        settled = _oracle(_WEEK, counted=lambda p: p.status == "settled")
        assert _by(rows, "channel", "settled_rate_7d") == _rate(settled)

    def test_the_zero_policy_applies_to_the_rate(self, run: Any) -> None:
        result, _ = run(["rate_7d"], ["channel"])
        assert any("zero denominator" in w for w in result.warnings)
        result, _ = run(["rate_7d_zero"], ["channel"])
        assert not any("zero denominator" in w for w in result.warnings)

    def test_the_rate_counts_its_zero_denominators(self, run: Any) -> None:
        result, rows = run(["rate_7d"], ["channel"])
        assert result.zero_denominator_metrics == {"rate_7d": "rate_7d"}
        assert {row[zero_denominator_column("rate_7d")] for row in rows} == {0}

    def test_counted_conversions_have_no_denominator(self, run: Any) -> None:
        result, _ = run(["conversions_7d"], ["channel"])
        assert not result.zero_denominator_metrics
        assert zero_denominator_column("conversions_7d") not in result.sql

    def test_a_derived_metric_answers_for_the_rate_it_reads(self, run: Any) -> None:
        result, rows = run(["rate_7d_percent"], ["channel"])
        assert [w for w in result.warnings if "zero denominator" in w] == [
            zero_denominator_warning("rate_7d_percent")
        ]
        assert {row[zero_denominator_column("rate_7d_percent")] for row in rows} == {0}

    def test_a_derived_metric_reads_the_rate(self, run: Any) -> None:
        result, rows = run(["rate_7d_percent"], ["channel"])
        expected = _rate(_oracle(_WEEK))
        assert _by(rows, "channel", "rate_7d_percent") == {
            k: round(100 * v, 2) for k, v in expected.items()
        }
        assert set(result.conversions) == {"rate_7d"}


class TestQueryScope:
    def test_c5_a_filter_narrows_the_visits_only(self, run: Any) -> None:
        _, rows = run(["conversions_7d"], ["channel"], ["visited_at < '2024-01-03'"])
        early = [v for v in _visits() if v.at < datetime(2024, 1, 3)]
        # Without visit 2, purchase 1 is attributed to visit 1, still inside its window.
        assert _by(rows, "channel", "conversions_7d") == _oracle(_WEEK, visits=early)
        assert _by(rows, "channel", "conversions_7d") == {"ads": 1}

    def test_c5_a_purchase_after_the_range_counts_for_a_visit_inside_it(self, run: Any) -> None:
        _, rows = run(
            ["conversions_7d"],
            ["channel"],
            time_range={"dimension": "visited_at", "start": "2024-01-01", "end": "2024-01-04"},
        )
        early = [v for v in _visits() if v.at < datetime(2024, 1, 4)]
        assert _by(rows, "channel", "conversions_7d") == _oracle(_WEEK, visits=early)
        assert _by(rows, "channel", "conversions_7d")["seo"] == 2  # purchase 2 on 2024-01-10

    def test_c7_a_joined_dimension_groups_by_the_visits_value(self, run: Any) -> None:
        _, rows = run(["rate_7d"], ["country"])
        assert _by(rows, "country", "rate_7d") == _rate(
            _oracle(_WEEK, by=lambda v: v.country), by=lambda v: v.country
        )

    def test_by_month(self, run: Any) -> None:
        _, rows = run(["conversions_7d", "visits"], ["visit_month"])
        assert [(r["conversions_7d"], r["visits"]) for r in rows] == [(3, 5), (1, 3)]

    def test_c8_a_fanning_dimension_is_refused(self, run: Any) -> None:
        with pytest.raises(FanoutUnsafe, match="one_to_many"):
            run(["rate_7d"], ["view_id"])

    def test_c10_dedup_on_and_off_agree(self, run: Any) -> None:
        metrics = ["rate_7d", "conversions_7d", "visits", "rate_ever", "revenue_7d"]
        _, fused = run(metrics, ["channel"])
        _, apart = run(metrics, ["channel"], dedup=False)
        assert fused == apart


class TestMetadata:
    def test_the_result_names_how_purchases_were_matched(self, run: Any) -> None:
        result, _ = run(["rate_same_product"], ["channel"])
        metadata = QueryMetadata.from_compile_result(result)
        assert metadata.conversion["rate_same_product"].model_dump() == {
            "base_metric": "visits",
            "conversion_metric": "purchases",
            "entity": {"base": "user_id", "conversion": "user_id"},
            "base_time": "visited_at",
            "conversion_time": "purchased_at",
            "window": None,
            "calculation": "conversion_rate",
            "constant_properties": [{"base": "product", "conversion": "sku"}],
            "attribution": "last_touch",
        }
        assert result.resolved["rate_same_product"] == (
            "conversion(visits.visits, purchases.purchases, conversion_rate)"
        )

    def test_the_resolved_key_names_the_window(self, run: Any) -> None:
        result, _ = run(["conversions_7d"])
        assert result.resolved["conversions_7d"] == (
            "conversion(visits.visits, purchases.purchases, conversions, window=7 days)"
        )


def test_a_conversion_under_an_offset_is_refused() -> None:
    resolver = ContractResolver(
        bindings=[
            *_resolver().active_bindings(),
            _binding(
                "rate_7d_py",
                kind="offset",
                base="rate_7d",
                time_dimension="visited_at",
                offset="1y",
            ),
        ],
        guardrails=(),
        finality=(),
        assertions=(),
    )
    with pytest.raises(UnsupportedMeasure, match="under an offset"):
        compile(
            SemanticQuery(metrics=["rate_7d_py"], dimensions=["visit_month"]),
            resolver,
            _sources(),
            connection_dialects={"wh": "duckdb"},
        )
