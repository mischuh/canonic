"""The typed ``time_range`` of a query: its shape, its resolution to dates, and its CLI form."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from canonic.compiler.query import SemanticQuery, TimeRange, parse_time_range_flag
from canonic.compiler.time_range import resolve_time_range
from canonic.semantic.duration import Duration

_ANCHOR = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # a Tuesday


class TestShape:
    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({}, "exactly one of 'last' and 'start'/'end'"),
            ({"last": "7 days", "start": "2026-01-01"}, "exactly one of"),
            ({"last": "6 hours"}, "a day or longer"),
            ({"start": "2026-02-01", "end": "2026-01-01"}, "must be before end"),
            ({"start": "2026-01-01", "include_current": True}, "requires 'last'"),
            ({"last": "7 days", "unknown": 1}, "Extra inputs"),
        ],
    )
    def test_invalid_ranges(self, fields: dict[str, object], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            TimeRange(dimension="order_date", **fields)  # type: ignore[arg-type]

    def test_one_open_side_is_allowed(self) -> None:
        assert TimeRange(dimension="d", start=date(2026, 1, 1)).end is None
        assert TimeRange(dimension="d", end=date(2026, 1, 1)).start is None

    def test_query_serializes_the_canonical_duration(self) -> None:
        query = SemanticQuery(metrics=["m"], time_range={"dimension": "d", "last": "90d"})
        assert query.model_dump(mode="json")["time_range"] == {
            "dimension": "d",
            "last": "90 days",
            "start": None,
            "end": None,
            "include_current": False,
        }


class TestResolution:
    @pytest.mark.parametrize(
        ("last", "start", "end"),
        [
            ("90 days", date(2026, 7, 22), date(2026, 10, 20)),
            ("2 weeks", date(2026, 10, 5), date(2026, 10, 19)),
            ("3 months", date(2026, 7, 1), date(2026, 10, 1)),
            ("1 quarter", date(2026, 7, 1), date(2026, 10, 1)),
            ("1 year", date(2025, 1, 1), date(2026, 1, 1)),
        ],
    )
    def test_last_counts_whole_units_before_the_current_one(
        self, last: str, start: date, end: date
    ) -> None:
        resolved = resolve_time_range(TimeRange(dimension="d", last=Duration.parse(last)), _ANCHOR)
        assert (resolved.start, resolved.end) == (start, end)

    def test_include_current_ends_after_the_anchor_day(self) -> None:
        resolved = resolve_time_range(
            TimeRange(dimension="d", last=Duration.parse("90 days"), include_current=True), _ANCHOR
        )
        assert (resolved.start, resolved.end) == (date(2026, 7, 22), date(2026, 10, 21))

    def test_the_anchor_day_is_read_in_the_offset_of_as_of(self) -> None:
        late_in_berlin = datetime(2026, 10, 20, 23, 30, tzinfo=timezone(timedelta(hours=2)))
        resolved = resolve_time_range(
            TimeRange(dimension="d", last=Duration.parse("1 day")), late_in_berlin
        )
        assert (resolved.start, resolved.end) == (date(2026, 10, 19), date(2026, 10, 20))

    def test_explicit_dates_pass_through(self) -> None:
        resolved = resolve_time_range(
            TimeRange(dimension="d", start=date(2026, 1, 1), end=date(2026, 4, 1)), None
        )
        assert (resolved.start, resolved.end, resolved.anchor) == (
            date(2026, 1, 1),
            date(2026, 4, 1),
            None,
        )

    def test_predicate_and_metadata(self) -> None:
        resolved = resolve_time_range(
            TimeRange(dimension="order_date", last=Duration.parse("90d")), _ANCHOR
        )
        assert resolved.predicate() == ("order_date >= '2026-07-22' AND order_date < '2026-10-20'")
        assert resolved.metadata().anchor == "2026-10-20T08:00:00+00:00"
        assert resolved.as_explicit() == TimeRange(
            dimension="order_date", start=date(2026, 7, 22), end=date(2026, 10, 20)
        )

    def test_without_as_of_the_anchor_is_now(self) -> None:
        resolved = resolve_time_range(TimeRange(dimension="d", last=Duration.parse("1d")), None)
        assert resolved.anchor is not None
        assert resolved.end == resolved.anchor.date()


class TestFlag:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("order_date:last:90d", TimeRange(dimension="order_date", last=Duration.parse("90d"))),
            (
                "order_date:last:3 months:current",
                TimeRange(
                    dimension="order_date", last=Duration.parse("3 months"), include_current=True
                ),
            ),
            (
                "order_date:2026-01-01..2026-04-01",
                TimeRange(dimension="order_date", start=date(2026, 1, 1), end=date(2026, 4, 1)),
            ),
            ("order_date:..2026-04-01", TimeRange(dimension="order_date", end=date(2026, 4, 1))),
        ],
    )
    def test_forms(self, raw: str, expected: TimeRange) -> None:
        assert parse_time_range_flag(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["order_date", "order_date:2026-01-01", "order_date:last:90d:previous", ":last:1d"]
    )
    def test_rejects_malformed_values(self, raw: str) -> None:
        with pytest.raises(ValueError, match="time range"):
            parse_time_range_flag(raw)
