"""A length of time written as text: ``"7 days"``, ``"1 month"``, ``"90d"``.

Used wherever a contract or a query names a span of calendar time: the trailing window of a
``cumulative`` binding and the ``last`` of a query's ``time_range``. Month, quarter and year
arithmetic clamps to the end of the month, so 31 January plus one month is the last day of
February, the same as every supported warehouse except SQLite, which the compiler corrects.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

__all__ = ["Duration", "DurationField", "DurationUnit", "add_to_date", "floor_date"]


class DurationUnit(StrEnum):
    """The calendar unit of a :class:`Duration`, shortest first."""

    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"

    @property
    def months(self) -> int | None:
        """How many months one unit is, or ``None`` for a unit of fixed length."""
        return {DurationUnit.MONTH: 1, DurationUnit.QUARTER: 3, DurationUnit.YEAR: 12}.get(self)

    @property
    def rank(self) -> int:
        """Position from the shortest unit, so units compare by length."""
        return list(DurationUnit).index(self)


_SHORT_UNITS: dict[str, DurationUnit] = {
    "d": DurationUnit.DAY,
    "w": DurationUnit.WEEK,
    "m": DurationUnit.MONTH,
    "q": DurationUnit.QUARTER,
    "y": DurationUnit.YEAR,
}
_LONG = re.compile(r"^\s*(\d+)\s+([a-z]+?)s?\s*$")
_SHORT = re.compile(r"^\s*(\d+)([dwmqy])\s*$")


@dataclass(frozen=True, slots=True, order=False)
class Duration:
    """A positive whole number of one calendar unit."""

    count: int
    unit: DurationUnit

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"a duration must be at least 1 {self.unit.value}, got {self.count}")

    @classmethod
    def parse(cls, text: str) -> Duration:
        """Read ``"7 days"``, ``"1 month"`` or the short form ``"90d"``.

        The short form knows ``d``, ``w``, ``m``, ``q`` and ``y``. ``m`` is a month, never a
        minute, because the short form is what pack manifests already write for day windows.
        """
        lowered = text.lower()
        short = _SHORT.match(lowered)
        if short:
            return cls(int(short.group(1)), _SHORT_UNITS[short.group(2)])
        long = _LONG.match(lowered)
        if long:
            try:
                unit = DurationUnit(long.group(2))
            except ValueError:
                pass
            else:
                return cls(int(long.group(1)), unit)
        units = ", ".join(u.value for u in DurationUnit)
        raise ValueError(f"{text!r} is not a duration; write '<n> <unit>' with unit one of {units}")

    def __str__(self) -> str:
        suffix = "" if self.count == 1 else "s"
        return f"{self.count} {self.unit.value}{suffix}"

    @property
    def months(self) -> int | None:
        """The whole duration in months, or ``None`` for a unit of fixed length."""
        per_unit = self.unit.months
        return None if per_unit is None else per_unit * self.count

    @property
    def days(self) -> int | None:
        """The whole duration in days, or ``None`` for a unit that is not a whole day."""
        if self.unit is DurationUnit.DAY:
            return self.count
        if self.unit is DurationUnit.WEEK:
            return 7 * self.count
        return None


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        return Duration.parse(value)
    return value


#: A pydantic field type that reads a :class:`Duration` from text and writes it canonically.
DurationField = Annotated[
    Duration,
    BeforeValidator(_coerce),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "examples": ["7 days", "1 month", "90d"]}),
]


def add_to_date(day: date, duration: Duration, *, sign: int = 1) -> date:
    """``day`` moved by ``duration`` forwards (``sign=1``) or backwards (``sign=-1``).

    Months clamp to the end of the target month. Units finer than a day are refused, since a
    date has no time of day to move.
    """
    months = duration.months
    if months is not None:
        index = day.year * 12 + (day.month - 1) + sign * months
        year, month = divmod(index, 12)
        last = calendar.monthrange(year, month + 1)[1]
        return date(year, month + 1, min(day.day, last))
    days = duration.days
    if days is None:
        raise ValueError(f"cannot move a date by {duration}, which is finer than a day")
    return day + timedelta(days=sign * days)


def floor_date(day: date, unit: DurationUnit) -> date:
    """The first day of the ``unit`` period containing ``day``. A week starts on Monday."""
    if unit is DurationUnit.DAY:
        return day
    if unit is DurationUnit.WEEK:
        return day - timedelta(days=day.weekday())
    if unit is DurationUnit.MONTH:
        return day.replace(day=1)
    if unit is DurationUnit.QUARTER:
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    if unit is DurationUnit.YEAR:
        return day.replace(month=1, day=1)
    raise ValueError(f"cannot floor a date to a {unit.value}")
