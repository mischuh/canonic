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

    def test_a_probe_carries_the_time_format(self) -> None:
        source = _orders(
            Dimension(name="booked_week", column="booked", time_grain="week", time_format="%G-%V")
        )
        compiled = _compile(filters=["booked_week >= '2026-40'"], source=source)

        (probe,) = compiled.probe_columns
        assert probe.time_format is not None
        assert probe.time_format.format == "%G-%V"

    def test_a_label_that_does_not_sort_by_time_is_not_probed(self) -> None:
        # MAX('43/2025', '01/2026') is the older week, so the probe would name the wrong one.
        source = _orders(
            Dimension(name="booked_week", column="booked", time_grain="week", time_format="%V/%G")
        )
        compiled = _compile(filters=["booked_week IN ('01/2026')"], source=source)

        assert compiled.probe_columns == ()

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

    def test_grouping_by_another_column_keeps_the_probe(self) -> None:
        source = _orders(Dimension(name="order_day", column="created_at", granularity="day"))
        compiled = _compile(
            filters=["created_at >= '2026-10-01'"], dimensions=["booked_month"], source=source
        )

        assert [p.subject for p in compiled.probe_columns] == ["order_month", "order_day"]

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

    @pytest.mark.parametrize(
        ("dimensions", "filters"),
        [
            pytest.param(["order_month"], ["created_at >= '2026-10-01'"], id="day-under-month"),
            pytest.param(["order_day"], ["order_month >= '2026-10-01'"], id="month-under-day"),
        ],
    )
    def test_no_probe_on_the_column_of_a_grouped_time_dimension(
        self, dimensions: list[str], filters: list[str]
    ) -> None:
        source = _orders(Dimension(name="order_day", column="created_at", granularity="day"))
        compiled = _compile(filters=filters, dimensions=dimensions, source=source)

        assert not compiled.probe_columns
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


def _calendar(*, relationship: Relationship = Relationship.MANY_TO_ONE, name: str | None = None):
    """An ``orders`` source joined to a calendar table, and the calendar itself."""
    calendar = SemanticSource(
        name="calendar",
        connection="wh",
        table="calendar",
        grain=["day"],
        columns=[Column(name="day", type="date", nullable=False)],
        dimensions=[
            Dimension(name="cal_day", column="day", granularity="day"),
            Dimension(
                name="cal_month", expr="date_trunc('month', day)", type="date", time_grain="month"
            ),
        ],
    )
    orders = _orders().model_copy(
        update={
            "joins": [
                Join(
                    to="calendar",
                    on="orders.created_at = calendar.day",
                    relationship=relationship,
                    name=name,
                )
            ]
        }
    )
    return orders, calendar


def _compile_joined(
    filters: list[str],
    *,
    relationship: Relationship = Relationship.MANY_TO_ONE,
    name: str | None = None,
    dialect: str = "duckdb",
) -> CompileResult:
    orders, calendar = _calendar(relationship=relationship, name=name)
    return compile(
        SemanticQuery(metrics=["revenue"], filters=filters),
        _resolver(),
        [orders, calendar],
        connection_dialects={"wh": dialect},
    )


class TestJoinedSourceDimension:
    def test_dimension_on_a_many_to_one_join_is_probed(self) -> None:
        compiled = _compile_joined(["cal_day >= '2026-10-01'"])

        (probe,) = compiled.probe_columns
        assert (probe.subject, probe.grain) == ("calendar.cal_day", "day")
        assert probe.column == "_canonic_latest_period_calendar_cal_day"
        assert 'MAX("calendar"."day")' in compiled.sql

    def test_filter_on_the_joined_column_is_recognised(self) -> None:
        compiled = _compile_joined(["calendar.day >= '2026-10-01'"])

        assert [p.subject for p in compiled.probe_columns] == ["calendar.cal_day"]

    def test_filter_on_the_qualified_joined_dimension_is_recognised(self) -> None:
        compiled = _compile_joined(["calendar.cal_day >= '2026-10-01'"])

        assert [p.subject for p in compiled.probe_columns] == ["calendar.cal_day"]

    def test_filter_on_the_role_qualified_dimension_is_recognised(self) -> None:
        compiled = _compile_joined(["booked_on.cal_day >= '2026-10-01'"], name="booked_on")

        (probe,) = compiled.probe_columns
        assert probe.subject == "booked_on.cal_day"
        assert 'MAX("booked_on"."day")' in compiled.sql

    def test_filter_on_the_qualified_derived_dimension_is_recognised(self) -> None:
        compiled = _compile_joined(["calendar.cal_month >= '2026-10-01'"])

        assert [p.subject for p in compiled.probe_columns] == ["calendar.cal_month"]

    def test_the_joined_dimension_is_read_through_the_join_alias(self) -> None:
        compiled = _compile_joined(["cal_day >= '2026-10-01'"], name="booked_on")

        (probe,) = compiled.probe_columns
        assert probe.subject == "booked_on.cal_day"
        assert 'MAX("booked_on"."day")' in compiled.sql

    def test_derived_dimension_on_the_joined_source_is_qualified_to_its_alias(self) -> None:
        compiled = _compile_joined(["cal_month >= '2026-10-01'"])

        (probe,) = compiled.probe_columns
        assert probe.subject == "calendar.cal_month"
        assert '"calendar"."day"' in compiled.sql

    def test_join_that_multiplies_rows_is_left_unchecked(self) -> None:
        compiled = _compile_joined(
            ["cal_day >= '2026-10-01'"], relationship=Relationship.ONE_TO_MANY
        )

        assert compiled.probe_columns == ()
        assert "_canonic_latest_period" not in compiled.sql

    def test_own_dimension_is_still_probed_behind_a_join_that_multiplies_rows(self) -> None:
        compiled = _compile_joined(
            ["created_at >= '2026-10-01'", "cal_day >= '2026-10-01'"],
            relationship=Relationship.ONE_TO_MANY,
        )

        assert [p.subject for p in compiled.probe_columns] == ["order_month"]

    def test_probe_still_fuses_into_the_metric_cte(self) -> None:
        compiled = _compile_joined(["cal_day >= '2026-10-01'"])

        assert "WITH" not in compiled.sql.upper()

    def test_open_day_on_the_joined_dimension_is_flagged(self) -> None:
        compiled = _compile_joined(["cal_day >= '2026-10-01'"])
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE orders (order_id INT, created_at DATE, booked TEXT, status TEXT, "
            "amount DOUBLE, props JSON)"
        )
        con.execute("CREATE TABLE calendar (day DATE)")
        con.execute("INSERT INTO calendar VALUES ('2026-10-16'), ('2026-10-17'), ('2026-10-18')")
        con.execute(
            "INSERT INTO orders VALUES (1, '2026-10-17', 'x', 'ok', 10.0, NULL), "
            "(2, '2026-10-18', 'x', 'ok', 5.0, NULL)"
        )
        cursor = con.execute(compiled.sql)
        raw = ResultSet(
            columns=[ResultColumn(name=d[0], type="any") for d in cursor.description],
            rows=[list(row) for row in cursor.fetchall()],
        )

        result, values = strip_probes(compiled, raw)
        (period,) = open_periods(compiled, result, datetime(2026, 10, 18, 12, 0), values)

        assert [c.name for c in result.columns] == ["revenue"]
        assert (period.dimension, period.grain, period.label) == (
            "calendar.cal_day",
            "day",
            "2026-10-18",
        )


class TestProbeSurvivesEveryDialect:
    """A double-quoted name is a string in some dialects, so the probe must be written for the
    dialect it is read back in."""

    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "snowflake", "databricks"])
    def test_probe_aggregates_a_column_and_not_a_string(self, dialect: str) -> None:
        import sqlglot
        from sqlglot import exp

        compiled = _compile(filters=["created_at >= '2026-10-01'"])
        compiled = compile(
            SemanticQuery(metrics=["revenue"], filters=["created_at >= '2026-10-01'"]),
            _resolver(),
            [_orders()],
            connection_dialects={"wh": dialect},
        )

        read_as = "spark" if dialect == "databricks" else dialect
        maxima = list(sqlglot.parse_one(compiled.sql, read=read_as).find_all(exp.Max))
        assert len(maxima) == 1
        assert isinstance(maxima[0].this, exp.Column), compiled.sql
        assert maxima[0].this.name.lower() == "created_at"


class TestProbeNeverBreaksAQuery:
    def test_column_that_needs_quoting_is_not_probed(self) -> None:
        source = SemanticSource(
            name="orders",
            connection="wh",
            table="orders",
            grain=["order_id"],
            columns=[
                Column(name="order_id", type="int", nullable=False),
                Column(name="Order Date", type="timestamp", nullable=False),
                Column(name="amount", type="decimal", nullable=False),
            ],
            measures=[Measure(name="total", expr="sum(amount)", additivity="additive")],
            dimensions=[Dimension(name="order_day", column="Order Date", granularity="day")],
        )

        compiled = _compile(filters=["order_day >= '2026-10-01'"], source=source)

        assert compiled.probe_columns == ()
        assert compiled.sql

    @pytest.mark.parametrize("dialect", ["postgres", "databricks", "snowflake"])
    def test_derived_dimension_compiles_in_every_dialect(self, dialect: str) -> None:
        source = _orders(
            Dimension(
                name="booked_day",
                expr="cast(created_at as date)",
                type="date",
                granularity="day",
            )
        )

        compiled = compile(
            SemanticQuery(metrics=["revenue"], filters=["booked_day >= '2026-10-01'"]),
            _resolver(),
            [source],
            connection_dialects={"wh": dialect},
        )

        assert compiled.sql
        assert {p.subject for p in compiled.probe_columns} <= {"booked_day"}
