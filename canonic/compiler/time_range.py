"""Resolve a query's typed ``time_range`` to two dates and the filter it compiles to.

The range is resolved once, before planning, so every leaf of a query reads the same two
dates and the compiled SQL never depends on the warehouse clock. A query pinned with
``as_of`` therefore compiles to the same SQL on every run and every dialect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from canonic.compiler.query import TimeRange
from canonic.compiler.result import TimeRangeMetadata
from canonic.exc import ValidationFailed
from canonic.semantic.duration import Duration, add_to_date, floor_date
from canonic.semantic.models import NormalizedType

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.semantic.models import SemanticSource

__all__ = ["ResolvedTimeRange", "anchor_day", "apply_time_range", "resolve_time_range"]

_TIME_TYPES = frozenset({NormalizedType.DATE, NormalizedType.TIMESTAMP})


@dataclass(frozen=True, slots=True)
class ResolvedTimeRange:
    """A ``time_range`` as two dates, ``start`` inclusive and ``end`` exclusive."""

    dimension: str
    start: date | None
    end: date | None
    last: Duration | None = None
    include_current: bool = False
    anchor: datetime | None = None

    def predicate(self) -> str:
        """The filter the range adds to the query, over the dimension's backing column."""
        bounds: list[str] = []
        if self.start is not None:
            bounds.append(f"{self.dimension} >= '{self.start.isoformat()}'")
        if self.end is not None:
            bounds.append(f"{self.dimension} < '{self.end.isoformat()}'")
        return " AND ".join(bounds)

    def as_explicit(self) -> TimeRange:
        """The same range written with dates only, so planning never resolves it again."""
        return TimeRange(dimension=self.dimension, start=self.start, end=self.end)

    def metadata(self) -> TimeRangeMetadata:
        return TimeRangeMetadata(
            dimension=self.dimension,
            start=self.start.isoformat() if self.start is not None else None,
            end=self.end.isoformat() if self.end is not None else None,
            last=str(self.last) if self.last is not None else None,
            include_current=self.include_current,
            anchor=self.anchor.isoformat() if self.anchor is not None else None,
        )


def anchor_day(as_of: datetime) -> date:
    """The calendar day of ``as_of`` in the offset it carries, UTC when it carries none."""
    return as_of.date()


def resolve_time_range(time_range: TimeRange, as_of: datetime | None) -> ResolvedTimeRange:
    """Turn ``last`` into dates counted back from ``as_of``, or the current UTC time without it.

    ``last: N unit`` is the ``N`` whole units before the unit that contains the anchor day.
    ``include_current`` moves the end to the day after the anchor day.
    """
    if time_range.last is None:
        return ResolvedTimeRange(
            dimension=time_range.dimension, start=time_range.start, end=time_range.end
        )
    anchor = as_of if as_of is not None else datetime.now(UTC)
    day = anchor_day(anchor)
    end = floor_date(day, time_range.last.unit)
    start = add_to_date(end, time_range.last, sign=-1)
    if time_range.include_current:
        end = day + timedelta(days=1)
    return ResolvedTimeRange(
        dimension=time_range.dimension,
        start=start,
        end=end,
        last=time_range.last,
        include_current=time_range.include_current,
        anchor=anchor,
    )


def check_time_range_dimension(
    time_range: TimeRange, sources_by_name: dict[str, SemanticSource]
) -> None:
    """Refuse a range on a dimension that is not one ``date`` or ``timestamp`` column.

    Only a dimension declared somewhere is checked. A name no source declares is left to
    filter binding, which reports it the same way as any other unknown name.
    """
    name = time_range.dimension.rpartition(".")[2]
    for source in sources_by_name.values():
        for dim in source.dimensions:
            if dim.name != name and name not in dim.aliases:
                continue
            columns = dim.backing_columns()
            if len(columns) != 1 or dim.json_path is not None:
                raise ValidationFailed(
                    f"time_range dimension {time_range.dimension!r} must be backed by exactly "
                    f"one column"
                )
            (column,) = columns
            column_type = next((c.type for c in source.columns if c.name == column), None)
            if column_type not in _TIME_TYPES:
                shown = column_type.value if column_type is not None else None
                raise ValidationFailed(
                    f"time_range dimension {time_range.dimension!r} reads column {column!r} of "
                    f"type {shown!r}; it must be date or timestamp"
                )


def apply_time_range(
    query: SemanticQuery, sources_by_name: dict[str, SemanticSource]
) -> tuple[SemanticQuery, ResolvedTimeRange | None]:
    """The query to plan, with its ``time_range`` resolved and added to ``filters``.

    The range becomes one more filter, so every kind treats it like the equivalent filter.
    It stays on the query as explicit dates, for the kinds that also need it typed.
    """
    if query.time_range is None:
        return query, None
    check_time_range_dimension(query.time_range, sources_by_name)
    resolved = resolve_time_range(query.time_range, query.as_of)
    planned = query.model_copy(
        update={
            "filters": [*query.filters, resolved.predicate()],
            "time_range": resolved.as_explicit(),
        }
    )
    return planned, resolved
