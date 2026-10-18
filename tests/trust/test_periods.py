"""A served result whose latest period has not ended."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

import pytest

from canonic.compiler.result import (
    PROBE_LATEST_PERIOD,
    CompileResult,
    FinalityMetadata,
    ProbeColumn,
    TimeDimension,
)
from canonic.connectors.base import ResultColumn, ResultSet
from canonic.semantic.models import Dimension
from canonic.trust.models import TrustTier
from canonic.trust.periods import floor_to_grain, open_periods, parse_period_start, period_end
from canonic.trust.scorer import trust_for_compiled

NOW = datetime(2026, 10, 18, 12, 0)


def _compiled(*time_dimensions: TimeDimension, finality: FinalityMetadata | None = None):
    return CompileResult(
        sql="SELECT 1",
        dialect="postgres",
        resolved={},
        time_dimensions=time_dimensions,
        finality=finality,
    )


def _result(column: str, *values: object) -> ResultSet:
    return ResultSet(columns=[ResultColumn(name=column, type="date")], rows=[[v] for v in values])


class TestPeriodEnd:
    @pytest.mark.parametrize(
        ("start", "grain", "end"),
        [
            (datetime(2026, 10, 18, 9), "hour", datetime(2026, 10, 18, 10)),
            (datetime(2026, 10, 18), "day", datetime(2026, 10, 19)),
            (datetime(2026, 10, 12), "week", datetime(2026, 10, 19)),
            (datetime(2026, 10, 1), "month", datetime(2026, 11, 1)),
            (datetime(2026, 12, 1), "month", datetime(2027, 1, 1)),
            (datetime(2026, 10, 1), "quarter", datetime(2027, 1, 1)),
            (datetime(2026, 1, 1), "quarter", datetime(2026, 4, 1)),
            (datetime(2026, 1, 1), "year", datetime(2027, 1, 1)),
        ],
    )
    def test_end_of_each_grain(self, start: datetime, grain: str, end: datetime) -> None:
        assert period_end(start, grain) == end


class TestParsePeriodStart:
    @pytest.mark.parametrize(
        ("value", "grain", "start"),
        [
            (datetime(2026, 10, 1), "month", datetime(2026, 10, 1)),
            (datetime(2026, 10, 1, 2, tzinfo=UTC), "day", datetime(2026, 10, 1, 2)),
            (date(2026, 10, 1), "month", datetime(2026, 10, 1)),
            ("2026-10-01", "month", datetime(2026, 10, 1)),
            ("2026-10-01 00:00:00", "day", datetime(2026, 10, 1)),
            ("2026-10", "month", datetime(2026, 10, 1)),
            ("2026", "year", datetime(2026, 1, 1)),
            (2026, "year", datetime(2026, 1, 1)),
            ("2026-Q4", "quarter", datetime(2026, 10, 1)),
            ("2026Q2", "quarter", datetime(2026, 4, 1)),
            ("2026-W43", "week", datetime(2026, 10, 19)),
        ],
    )
    def test_readable_values(self, value: object, grain: str, start: datetime) -> None:
        assert parse_period_start(value, grain) == start

    @pytest.mark.parametrize("value", ["October", "2026-13", "", None, 3.5, "Q4 2026", True])
    def test_unreadable_values_are_left_alone(self, value: object) -> None:
        assert parse_period_start(value, "month") is None


class TestOpenPeriods:
    def test_latest_period_not_ended_is_open(self) -> None:
        compiled = _compiled(TimeDimension("order_month", "month"))
        result = _result("order_month", datetime(2026, 9, 1), datetime(2026, 10, 1))

        (period,) = open_periods(compiled, result, NOW)

        assert (period.dimension, period.grain, period.label) == ("order_month", "month", "2026-10")
        assert "order_month: the month 2026-10 has not ended" in period.warning

    def test_ended_latest_period_is_not_open(self) -> None:
        compiled = _compiled(TimeDimension("order_month", "month"))

        assert open_periods(compiled, _result("order_month", datetime(2026, 9, 1)), NOW) == []

    def test_only_the_latest_period_counts(self) -> None:
        """A row order that ends on an old period must not hide the open one."""
        compiled = _compiled(TimeDimension("order_month", "month"))
        result = _result("order_month", datetime(2026, 10, 1), datetime(2026, 1, 1))

        assert len(open_periods(compiled, result, NOW)) == 1

    def test_period_ending_exactly_now_has_ended(self) -> None:
        compiled = _compiled(TimeDimension("d", "day"))

        assert (
            open_periods(compiled, _result("d", datetime(2026, 10, 17)), datetime(2026, 10, 18))
            == []
        )

    def test_future_period_is_open(self) -> None:
        compiled = _compiled(TimeDimension("order_month", "month"))

        assert open_periods(compiled, _result("order_month", datetime(2027, 1, 1)), NOW)

    def test_label_dimension_is_read_from_its_label(self) -> None:
        compiled = _compiled(TimeDimension("month_label", "month"))

        assert open_periods(compiled, _result("month_label", "2026-09", "2026-10"), NOW)
        assert not open_periods(compiled, _result("month_label", "2026-08", "2026-09"), NOW)

    def test_unreadable_labels_leave_the_check_inactive(self) -> None:
        compiled = _compiled(TimeDimension("month_label", "month"))

        assert open_periods(compiled, _result("month_label", "Q4-ish", None), NOW) == []

    def test_timezone_aware_clock_is_compared_in_utc(self) -> None:
        compiled = _compiled(TimeDimension("d", "day"))
        result = _result("d", datetime(2026, 10, 18))

        assert open_periods(compiled, result, datetime(2026, 10, 18, 23, tzinfo=UTC))
        assert not open_periods(compiled, result, datetime(2026, 10, 19, 1, tzinfo=UTC))

    def test_every_open_time_dimension_is_reported(self) -> None:
        compiled = _compiled(TimeDimension("a", "month"), TimeDimension("b", "year"))
        result = ResultSet(
            columns=[ResultColumn(name="a", type="date"), ResultColumn(name="b", type="date")],
            rows=[[datetime(2026, 10, 1), datetime(2026, 1, 1)]],
        )

        assert [p.dimension for p in open_periods(compiled, result, NOW)] == ["a", "b"]

    def test_inactive_without_a_result_or_a_clock(self) -> None:
        compiled = _compiled(TimeDimension("order_month", "month"))
        result = _result("order_month", datetime(2026, 10, 1))

        assert open_periods(compiled, None, NOW) == []
        assert open_periods(compiled, result, None) == []

    def test_inactive_when_a_dimension_is_not_in_the_result(self) -> None:
        compiled = _compiled(TimeDimension("order_month", "month"))

        assert open_periods(compiled, _result("other", datetime(2026, 10, 1)), NOW) == []

    def test_inactive_when_a_finality_rule_governs_the_query(self) -> None:
        finality = FinalityMetadata(
            watermark="2026-10-17T00:00:00Z", sources_used=["a", "b"], result_flag="per_row"
        )
        compiled = _compiled(TimeDimension("order_month", "month"), finality=finality)

        assert open_periods(compiled, _result("order_month", datetime(2026, 10, 1)), NOW) == []


class TestTrustCap:
    def test_open_period_caps_the_tier_at_provisional(self) -> None:
        compiled = _compiled(TimeDimension("order_month", "month"))
        result = _result("order_month", datetime(2026, 10, 1))

        score = trust_for_compiled(compiled, result, now=NOW)

        assert score.tier is TrustTier.PROVISIONAL
        assert score.reasons == ("order_month: month 2026-10 has not ended",)

    def test_without_a_clock_the_tier_is_untouched(self) -> None:
        compiled = _compiled(TimeDimension("order_month", "month"))
        result = _result("order_month", datetime(2026, 10, 1))

        assert trust_for_compiled(compiled, result).tier is TrustTier.TRUSTED


class TestDimensionGrain:
    def test_time_grain_wins_over_granularity(self) -> None:
        dim = Dimension(name="m", column="c", granularity="day", time_grain="month")

        assert dim.period_grain == "month"

    def test_granularity_is_used_when_it_is_a_period(self) -> None:
        assert Dimension(name="m", column="c", granularity="Month").period_grain == "month"

    def test_unknown_grain_switches_the_check_off(self) -> None:
        assert Dimension(name="m", column="c", granularity="minute").period_grain is None
        assert Dimension(name="m", column="c").period_grain is None

    def test_invalid_time_grain_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="time_grain"):
            Dimension(name="m", column="c", time_grain="fortnight")


class TestFloorToGrain:
    @pytest.mark.parametrize(
        ("moment", "grain", "floor"),
        [
            (datetime(2026, 10, 18, 9, 30, 15), "hour", datetime(2026, 10, 18, 9)),
            (datetime(2026, 10, 18, 9, 30), "day", datetime(2026, 10, 18)),
            (datetime(2026, 10, 18, 9, 30), "week", datetime(2026, 10, 12)),
            (datetime(2026, 10, 12), "week", datetime(2026, 10, 12)),
            (datetime(2026, 10, 18, 9, 30), "month", datetime(2026, 10, 1)),
            (datetime(2026, 11, 30), "quarter", datetime(2026, 10, 1)),
            (datetime(2026, 2, 3), "quarter", datetime(2026, 1, 1)),
            (datetime(2026, 10, 18, 9, 30), "year", datetime(2026, 1, 1)),
        ],
    )
    def test_start_of_the_containing_period(
        self, moment: datetime, grain: str, floor: datetime
    ) -> None:
        assert floor_to_grain(moment, grain) == floor


class TestOpenPeriodFromProbe:
    def _compiled(self, grain: str = "month"):
        probe = ProbeColumn("_p", PROBE_LATEST_PERIOD, "order_month", grain)
        return CompileResult(
            sql="SELECT 1", dialect="postgres", resolved={}, probe_columns=(probe,)
        )

    def _result(self) -> ResultSet:
        return ResultSet(columns=[ResultColumn(name="revenue", type="int")], rows=[[1]])

    def test_latest_value_inside_the_current_period_is_open(self) -> None:
        probes = {(PROBE_LATEST_PERIOD, "order_month"): datetime(2026, 10, 17, 9, 30)}

        (period,) = open_periods(self._compiled(), self._result(), NOW, probes)

        assert period.start == datetime(2026, 10, 1)
        assert period.label == "2026-10"

    def test_latest_value_in_an_ended_period_is_not_open(self) -> None:
        probes = {(PROBE_LATEST_PERIOD, "order_month"): datetime(2026, 9, 30, 23)}

        assert open_periods(self._compiled(), self._result(), NOW, probes) == []

    def test_no_value_means_no_signal(self) -> None:
        compiled, result = self._compiled(), self._result()

        assert (
            open_periods(compiled, result, NOW, {(PROBE_LATEST_PERIOD, "order_month"): None}) == []
        )
        assert open_periods(compiled, result, NOW, {}) == []
        assert open_periods(compiled, result, NOW, None) == []

    def test_a_finality_rule_still_takes_precedence(self) -> None:
        finality = FinalityMetadata(
            watermark="2026-10-17T00:00:00Z", sources_used=["a"], result_flag="per_row"
        )
        compiled = dataclasses.replace(self._compiled(), finality=finality)
        probes = {(PROBE_LATEST_PERIOD, "order_month"): datetime(2026, 10, 17)}

        assert open_periods(compiled, self._result(), NOW, probes) == []

    def test_open_probe_period_caps_trust(self) -> None:
        probes = {(PROBE_LATEST_PERIOD, "order_month"): datetime(2026, 10, 17)}

        score = trust_for_compiled(self._compiled(), self._result(), now=NOW, probes=probes)

        assert score.tier is TrustTier.PROVISIONAL
        assert score.reasons == ("order_month: month 2026-10 has not ended",)
