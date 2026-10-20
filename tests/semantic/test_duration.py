"""Durations written as text, and the calendar arithmetic every window and range relies on."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import BaseModel

from canonic.semantic.duration import Duration, DurationField, DurationUnit, add_to_date, floor_date


class TestParse:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("7 days", Duration(7, DurationUnit.DAY)),
            ("1 day", Duration(1, DurationUnit.DAY)),
            ("1 Month", Duration(1, DurationUnit.MONTH)),
            ("  3  quarters ", Duration(3, DurationUnit.QUARTER)),
            ("2 weeks", Duration(2, DurationUnit.WEEK)),
            ("6 hours", Duration(6, DurationUnit.HOUR)),
            ("90d", Duration(90, DurationUnit.DAY)),
            ("12m", Duration(12, DurationUnit.MONTH)),
            ("1y", Duration(1, DurationUnit.YEAR)),
        ],
    )
    def test_long_and_short_forms(self, text: str, expected: Duration) -> None:
        assert Duration.parse(text) == expected

    @pytest.mark.parametrize("text", ["", "days", "7", "7 fortnights", "-3 days", "1.5 days", "7x"])
    def test_rejects_anything_else(self, text: str) -> None:
        with pytest.raises(ValueError, match="duration"):
            Duration.parse(text)

    def test_zero_is_not_a_duration(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            Duration.parse("0 days")

    @pytest.mark.parametrize(
        ("text", "canonical"), [("90d", "90 days"), ("1 months", "1 month"), ("2 year", "2 years")]
    )
    def test_canonical_form(self, text: str, canonical: str) -> None:
        assert str(Duration.parse(text)) == canonical

    def test_pydantic_field_reads_text_and_writes_the_canonical_form(self) -> None:
        class Model(BaseModel):
            window: DurationField

        model = Model(window="30d")  # type: ignore[arg-type]
        assert model.window == Duration(30, DurationUnit.DAY)
        assert model.model_dump(mode="json") == {"window": "30 days"}
        assert Model.model_json_schema()["properties"]["window"]["type"] == "string"


class TestArithmetic:
    @pytest.mark.parametrize(
        ("day", "text", "sign", "expected"),
        [
            (date(2026, 1, 31), "1 month", 1, date(2026, 2, 28)),
            (date(2024, 1, 31), "1 month", 1, date(2024, 2, 29)),
            (date(2024, 2, 29), "1 year", 1, date(2025, 2, 28)),
            (date(2026, 3, 31), "1 month", -1, date(2026, 2, 28)),
            (date(2026, 5, 31), "1 quarter", -1, date(2026, 2, 28)),
            (date(2026, 12, 15), "2 months", 1, date(2027, 2, 15)),
            (date(2026, 3, 1), "7 days", -1, date(2026, 2, 22)),
            (date(2026, 1, 15), "2 weeks", 1, date(2026, 1, 29)),
        ],
    )
    def test_add_clamps_to_the_end_of_the_month(
        self, day: date, text: str, sign: int, expected: date
    ) -> None:
        assert add_to_date(day, Duration.parse(text), sign=sign) == expected

    def test_a_date_cannot_move_by_hours(self) -> None:
        with pytest.raises(ValueError, match="finer than a day"):
            add_to_date(date(2026, 1, 1), Duration.parse("6 hours"))

    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            (DurationUnit.DAY, date(2026, 10, 20)),
            (DurationUnit.WEEK, date(2026, 10, 19)),
            (DurationUnit.MONTH, date(2026, 10, 1)),
            (DurationUnit.QUARTER, date(2026, 10, 1)),
            (DurationUnit.YEAR, date(2026, 1, 1)),
        ],
    )
    def test_floor_to_the_start_of_the_period(self, unit: DurationUnit, expected: date) -> None:
        assert floor_date(date(2026, 10, 20), unit) == expected

    def test_units_compare_by_length(self) -> None:
        assert DurationUnit.HOUR.rank < DurationUnit.DAY.rank < DurationUnit.YEAR.rank
