"""``time_format``: how a label dimension spells its periods."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from canonic.exc import InvalidTimeFormat
from canonic.semantic.models import Dimension
from canonic.semantic.time_format import TimeFormat


class TestParse:
    @pytest.mark.parametrize(
        ("format", "grain", "label", "start"),
        [
            ("%G-%V", "week", "2026-43", datetime(2026, 10, 19)),
            ("%G-%V", "week", "2026-05", datetime(2026, 1, 26)),
            ("%G-%V", "week", "2026-01", datetime(2025, 12, 29)),
            ("%GW%V", "week", "2026W43", datetime(2026, 10, 19)),
            ("KW %V/%G", "week", "KW 43/2026", datetime(2026, 10, 19)),
            ("%Y%m", "month", "202610", datetime(2026, 10, 1)),
            ("%m/%Y", "month", "10/2026", datetime(2026, 10, 1)),
            ("%Y-Q%q", "quarter", "2026-Q4", datetime(2026, 10, 1)),
            ("Q%q %Y", "quarter", "Q2 2026", datetime(2026, 4, 1)),
            ("FY%Y", "year", "FY2026", datetime(2026, 1, 1)),
            ("%d.%m.%Y", "day", "19.10.2026", datetime(2026, 10, 19)),
            ("%Y%m%d%H", "hour", "2026101909", datetime(2026, 10, 19, 9)),
            ("%Y-%m 100%%", "month", "2026-10 100%", datetime(2026, 10, 1)),
        ],
    )
    def test_reads_a_label(self, format: str, grain: str, label: str, start: datetime) -> None:
        assert TimeFormat.compile(format, grain).parse(label) == start

    @pytest.mark.parametrize(
        ("format", "grain", "label"),
        [
            ("%G-%V", "week", "2026-W43"),  # the literal W is not in the format
            ("%G-%V", "week", "2026-4"),  # every placeholder has a fixed width
            ("%G-%V", "week", "2026-54"),  # no year has 54 weeks
            ("%G-%V", "week", "2025-53"),  # 2025 has 52 ISO weeks
            ("%Y%m", "month", "202613"),
            ("%Y%m", "month", "2026100"),
            ("%Y-Q%q", "quarter", "2026-Q5"),
        ],
    )
    def test_a_label_that_does_not_fit_is_none(self, format: str, grain: str, label: str) -> None:
        assert TimeFormat.compile(format, grain).parse(label) is None

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert TimeFormat.compile("%G-%V", "week").parse(" 2026-43 ") == datetime(2026, 10, 19)


class TestRender:
    @pytest.mark.parametrize(
        ("format", "grain", "start", "label"),
        [
            ("%G-%V", "week", datetime(2025, 12, 29), "2026-01"),
            ("%Y-Q%q", "quarter", datetime(2026, 10, 1), "2026-Q4"),
            ("%m/%Y", "month", datetime(2026, 3, 1), "03/2026"),
        ],
    )
    def test_renders_the_label_of_a_period(
        self, format: str, grain: str, start: datetime, label: str
    ) -> None:
        assert TimeFormat.compile(format, grain).render(start) == label


class TestSortsChronologically:
    @pytest.mark.parametrize(
        ("format", "grain", "sorts"),
        [
            ("%G-%V", "week", True),
            ("%Y%m", "month", True),
            ("%V/%G", "week", False),
            ("%m/%Y", "month", False),
            ("%d.%m.%Y", "day", False),
        ],
    )
    def test_most_significant_first_sorts(self, format: str, grain: str, sorts: bool) -> None:
        assert TimeFormat.compile(format, grain).sorts_chronologically is sorts


class TestCompile:
    @pytest.mark.parametrize(
        ("format", "grain", "match"),
        [
            ("%Y-%V", "week", "a week label needs exactly %G, %V"),
            ("%G-%m", "month", "a month label needs exactly %Y, %m"),
            ("%Y-%m-%d", "month", "does not fit time_grain 'month'"),
            ("%Y", "quarter", "a quarter label needs exactly %Y, %q"),
            ("%b %Y", "month", "unsupported placeholder %b"),
            ("%Y%m%m", "month", "uses %m twice"),
            ("%Y%m%", "month", "lone '%'"),
        ],
    )
    def test_rejects_a_format_that_does_not_fit(self, format: str, grain: str, match: str) -> None:
        with pytest.raises(InvalidTimeFormat, match=match):
            TimeFormat.compile(format, grain)


class TestDimension:
    def test_period_format_is_compiled_from_the_dimension(self) -> None:
        dim = Dimension(name="w", column="w", time_grain="week", time_format="%G-%V")

        assert dim.period_format is not None
        assert dim.period_format.parse("2026-43") == datetime(2026, 10, 19)

    def test_without_time_format_there_is_no_period_format(self) -> None:
        assert Dimension(name="w", column="w", time_grain="week").period_format is None

    def test_time_format_requires_time_grain(self) -> None:
        with pytest.raises(ValidationError, match="time_format without time_grain"):
            Dimension(name="w", column="w", time_format="%G-%V")

    def test_an_invalid_time_format_is_a_validation_error_of_the_dimension(self) -> None:
        with pytest.raises(ValidationError, match="dimension 'w': time_format '%Y-%V'"):
            Dimension(name="w", column="w", time_grain="week", time_format="%Y-%V")
