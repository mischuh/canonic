"""A query that only filters on a time dimension still learns its latest period."""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.compiler.result import PROBE_LATEST_PERIOD, CompileResult
from canonic.connectors.base import ResultColumn, ResultSet
from canonic.contracts.models import (
    AppliesTo,
    BindingKind,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.core.probes import strip_probes
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource
from canonic.trust.periods import open_periods

_ROWS = [
    (1, "2026-09-03 10:00:00", "2026-09", "ok", 10.0),
    (2, "2026-10-17 09:30:00", "2026-10", "ok", 5.0),
    (3, "2026-11-20 08:00:00", "2026-11", "test", 99.0),
]


def _orders(*extra: Dimension) -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="wh",
        table="orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="int", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
            Column(name="booked", type="string", nullable=False),
            Column(name="status", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="props", type="json", nullable=True),
        ],
        measures=[
            Measure(name="total", expr="sum(amount)", additivity="additive"),
            Measure(name="cnt", expr="count(order_id)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="order_month", column="created_at", granularity="month"),
            Dimension(name="order_ts", column="created_at"),
            Dimension(name="booked_month", column="booked", time_grain="month"),
            Dimension(name="status", column="status"),
            *extra,
        ],
    )


def _resolver(guardrails: list[Guardrail] | None = None) -> ContractResolver:
    return ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue", canonical=CanonicalRef(source="orders", measure="total")
            ),
            MetricBinding(metric="orders", canonical=CanonicalRef(source="orders", measure="cnt")),
            MetricBinding(
                metric="aov",
                canonical=CanonicalRef(
                    kind=BindingKind.RATIO, numerator="revenue", denominator="orders"
                ),
            ),
        ],
        guardrails=guardrails or [],
    )


def _compile(
    *,
    filters: list[str],
    dimensions: list[str] | None = None,
    metrics: tuple[str, ...] = ("revenue",),
    source: SemanticSource | None = None,
    guardrails: list[Guardrail] | None = None,
) -> CompileResult:
    return compile(
        SemanticQuery(metrics=list(metrics), dimensions=dimensions or [], filters=filters),
        _resolver(guardrails),
        [source or _orders()],
        connection_dialects={"wh": "duckdb"},
    )


def _run(compiled: CompileResult) -> tuple[ResultSet, dict[tuple[str, str], object]]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (order_id INT, created_at TIMESTAMP, booked TEXT, "
        "status TEXT, amount DOUBLE)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", _ROWS)
    cursor = con.execute(compiled.sql)
    raw = ResultSet(
        columns=[ResultColumn(name=d[0], type="any") for d in cursor.description],
        rows=[list(row) for row in cursor.fetchall()],
    )
    return strip_probes(compiled, raw)


class TestProbeIsPlanned:
    def test_filter_on_a_time_dimension_adds_a_probe(self) -> None:
        compiled = _compile(filters=["order_month >= '2026-10-01'"])

        (probe,) = compiled.probe_columns
        assert (probe.kind, probe.subject, probe.grain) == (
            PROBE_LATEST_PERIOD,
            "order_month",
            "month",
        )
        assert probe.column == "_canonic_latest_period_order_month"
        assert 'MAX("orders"."created_at")' in compiled.sql

    def test_filter_on_the_backing_column_is_recognised(self) -> None:
        compiled = _compile(filters=["created_at >= '2026-10-01'"])

        assert [p.subject for p in compiled.probe_columns] == ["order_month"]

    def test_label_dimension_with_a_time_grain_is_probed(self) -> None:
        compiled = _compile(filters=["booked_month >= '2026-10'"])

        (probe,) = compiled.probe_columns
        assert (probe.subject, probe.grain) == ("booked_month", "month")

    def test_each_filtered_dimension_gets_its_own_probe(self) -> None:
        compiled = _compile(filters=["order_month >= '2026-01-01'", "booked_month >= '2026-01'"])

        assert [p.subject for p in compiled.probe_columns] == ["order_month", "booked_month"]

    def test_probe_shares_the_cte_and_filters_of_the_metric(self) -> None:
        guardrail = Guardrail(
            id="no-tests",
            applies_to=AppliesTo(source="orders"),
            kind=GuardrailKind.MANDATORY_FILTER,
            filter="status != 'test'",
            severity=Severity.ERROR,
            rationale="Test orders are not revenue.",
        )

        compiled = _compile(
            filters=["created_at >= '2026-10-01'"],
            dimensions=["status"],
            metrics=("revenue", "aov"),
            guardrails=[guardrail],
        )

        assert compiled.sql.count("_leaf_") >= 1
        assert "_leaf_1" not in compiled.sql, "the probe fuses into the metric's CTE"
        assert "'test'" in compiled.sql

    def test_single_metric_stays_a_bare_statement(self) -> None:
        compiled = _compile(filters=["order_month >= '2026-10-01'"])

        assert "WITH" not in compiled.sql.upper()


class TestProbeIsSkipped:
    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"filters": ["status = 'ok'"]}, id="filter-on-a-non-time-dimension"),
            pytest.param({"filters": []}, id="no-filter"),
            pytest.param({"filters": ["order_ts >= '2026-10-01'"]}, id="no-known-grain"),
            pytest.param(
                {"filters": ["order_month >= '2026-10-01'"], "dimensions": ["order_month"]},
                id="grouped-by-the-dimension-already",
            ),
        ],
    )
    def test_no_probe(self, kwargs: dict[str, object]) -> None:
        compiled = _compile(**kwargs)  # type: ignore[arg-type]

        assert all(p.kind != PROBE_LATEST_PERIOD for p in compiled.probe_columns)
        assert "_canonic_latest_period" not in compiled.sql

    def test_json_key_dimension_is_not_probed(self) -> None:
        source = _orders(
            Dimension(
                name="json_day",
                column="props",
                json_path=["day"],
                type="date",
                granularity="day",
            )
        )

        compiled = _compile(filters=["json_day >= '2026-10-01'"], source=source)

        assert "_canonic_latest_period_json_day" not in compiled.sql

    def test_dimension_on_a_joined_source_is_not_probed(self) -> None:
        calendar = SemanticSource(
            name="calendar",
            connection="wh",
            table="calendar",
            grain=["day"],
            columns=[Column(name="day", type="date", nullable=False)],
            dimensions=[Dimension(name="cal_day", column="day", granularity="day")],
        )
        orders = _orders().model_copy(
            update={
                "joins": [
                    Join(
                        to="calendar",
                        on="orders.created_at = calendar.day",
                        relationship=Relationship.MANY_TO_ONE,
                    )
                ]
            }
        )

        compiled = compile(
            SemanticQuery(metrics=["revenue"], filters=["cal_day >= '2026-10-01'"]),
            _resolver(),
            [orders, calendar],
            connection_dialects={"wh": "duckdb"},
        )

        assert "_canonic_latest_period" not in compiled.sql


class TestProbeValue:
    NOW = datetime(2026, 10, 18, 12, 0)

    def test_latest_value_is_the_max_of_the_filtered_rows(self) -> None:
        compiled = _compile(filters=["created_at >= '2026-09-01'", "status = 'ok'"])

        result, values = _run(compiled)

        assert [c.name for c in result.columns] == ["revenue"]
        assert values[(PROBE_LATEST_PERIOD, "order_month")] == datetime(2026, 10, 17, 9, 30)

    def test_open_month_is_flagged(self) -> None:
        compiled = _compile(filters=["created_at >= '2026-09-01'", "status = 'ok'"])
        result, values = _run(compiled)

        (period,) = open_periods(compiled, result, self.NOW, values)

        assert (period.dimension, period.grain, period.label) == ("order_month", "month", "2026-10")

    def test_filter_that_stops_before_the_open_period_is_not_flagged(self) -> None:
        compiled = _compile(filters=["created_at >= '2026-09-01'", "created_at < '2026-10-01'"])
        result, values = _run(compiled)

        assert open_periods(compiled, result, self.NOW, values) == []

    def test_label_dimension_is_flagged_from_its_latest_label(self) -> None:
        compiled = _compile(filters=["booked_month >= '2026-09'", "status = 'ok'"])
        result, values = _run(compiled)

        (period,) = open_periods(compiled, result, self.NOW, values)

        assert period.label == "2026-10"

    def test_no_matching_rows_leave_the_check_inactive(self) -> None:
        compiled = _compile(filters=["created_at >= '2030-01-01'"])
        result, values = _run(compiled)

        assert values[(PROBE_LATEST_PERIOD, "order_month")] is None
        assert open_periods(compiled, result, self.NOW, values) == []

    def test_guardrail_rows_do_not_count_as_the_latest_period(self) -> None:
        guardrail = Guardrail(
            id="no-tests",
            applies_to=AppliesTo(source="orders"),
            kind=GuardrailKind.MANDATORY_FILTER,
            filter="status != 'test'",
            severity=Severity.ERROR,
            rationale="Test orders are not revenue.",
        )
        compiled = _compile(filters=["created_at >= '2026-09-01'"], guardrails=[guardrail])
        result, values = _run(compiled)

        # The November row is a test order the guardrail excludes, so October is the latest.
        assert values[(PROBE_LATEST_PERIOD, "order_month")] == datetime(2026, 10, 17, 9, 30)
        (period,) = open_periods(compiled, result, self.NOW, values)
        assert period.label == "2026-10"
