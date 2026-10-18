"""Detect a served result whose latest period has not ended yet."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult, TimeDimension
    from canonic.connectors.base import ResultSet

__all__ = ["OpenPeriod", "open_periods", "parse_period_start", "period_end"]

_YEAR = re.compile(r"^(\d{4})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.IGNORECASE)
_WEEK = re.compile(r"^(\d{4})-?W(\d{2})$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class OpenPeriod:
    """The latest period of a time dimension, which has not ended at the serving clock."""

    dimension: str
    grain: str
    start: datetime

    @property
    def label(self) -> str:
        """The period as a short, stable label such as ``2026-10`` or ``2026-10-18``."""
        if self.grain == "hour":
            return self.start.strftime("%Y-%m-%d %H:00")
        if self.grain == "month":
            return self.start.strftime("%Y-%m")
        if self.grain == "year":
            return self.start.strftime("%Y")
        if self.grain == "quarter":
            return f"{self.start.year}-Q{(self.start.month - 1) // 3 + 1}"
        return self.start.strftime("%Y-%m-%d")

    @property
    def warning(self) -> str:
        return (
            f"{self.dimension}: the {self.grain} {self.label} has not ended, "
            f"so its figures are incomplete"
        )

    @property
    def reason(self) -> str:
        return f"{self.dimension}: {self.grain} {self.label} has not ended"


def period_end(start: datetime, grain: str) -> datetime:
    """The first instant after the period of ``grain`` that begins at ``start``."""
    if grain == "hour":
        return start + timedelta(hours=1)
    if grain == "day":
        return start + timedelta(days=1)
    if grain == "week":
        return start + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[grain]
    month_index = start.year * 12 + (start.month - 1) + months
    return start.replace(year=month_index // 12, month=month_index % 12 + 1, day=1)


def parse_period_start(value: Any, grain: str) -> datetime | None:
    """The instant a dimension value's period begins, as naive UTC, or ``None`` if unreadable.

    A bucketed date or timestamp is already the start of its period. A label is read from the
    forms ``2026``, ``2026-10``, ``2026-Q4``, ``2026-W43`` and ISO dates. A value that fits none
    of them is left alone: the check is a signal, so it stays inactive instead of guessing.
    """
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, int) and not isinstance(value, bool) and grain == "year":
        return datetime(value, 1, 1) if 1 <= value <= 9999 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if match := _YEAR.match(text):
            return datetime(int(match[1]), 1, 1)
        if match := _MONTH.match(text):
            return datetime(int(match[1]), int(match[2]), 1)
        if match := _QUARTER.match(text):
            return datetime(int(match[1]), 3 * (int(match[2]) - 1) + 1, 1)
        if match := _WEEK.match(text):
            monday = date.fromisocalendar(int(match[1]), int(match[2]), 1)
            return datetime(monday.year, monday.month, monday.day)
        return parse_period_start(datetime.fromisoformat(text), grain)
    except ValueError:
        return None


def open_periods(
    compiled: CompileResult, result: ResultSet | None, now: datetime | None
) -> list[OpenPeriod]:
    """The time dimensions of ``compiled`` whose latest period in ``result`` has not ended.

    Inactive (empty) without a result or a clock, and whenever a finality rule governs the
    query: finality already says which rows are provisional, so this check defers to it.
    """
    if result is None or now is None or compiled.finality is not None:
        return []
    names = [column.name for column in result.columns]
    now_utc = now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now
    found: list[OpenPeriod] = []
    for time_dimension in compiled.time_dimensions:
        period = _latest_open(time_dimension, names, result.rows, now_utc)
        if period is not None:
            found.append(period)
    return found


def _latest_open(
    time_dimension: TimeDimension, names: list[str], rows: list[list[Any]], now: datetime
) -> OpenPeriod | None:
    if time_dimension.column not in names:
        return None
    index = names.index(time_dimension.column)
    starts = [
        start
        for row in rows
        if (start := parse_period_start(row[index], time_dimension.grain)) is not None
    ]
    if not starts:
        return None
    latest = max(starts)
    if period_end(latest, time_dimension.grain) <= now:
        return None
    return OpenPeriod(time_dimension.column, time_dimension.grain, latest)
