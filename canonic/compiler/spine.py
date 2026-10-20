"""The day spine: one row per calendar day between two bounds, built the same on every dialect.

A trailing window has a value on a day without rows of its own, so the days have to come
from somewhere other than the data. They are generated from a cross join of five digit
rows rather than a series function or a recursive CTE. Not every dialect has a series
function, Snowflake stops recursion at 100 levels and MySQL at 1000 by default, and a
recursive CTE would have to be declared on the statement's single outer ``WITH``. The digits
cover 100,000 days, and the spine keeps the latest of them when the data spans more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler._helpers import _active_adapter, _alias
from canonic.semantic.duration import DurationUnit

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["DAY_COLUMN", "MAX_SPINE_DAYS", "bounds_select", "digits_select", "spine_select"]

#: The column every spine row carries its day in.
DAY_COLUMN = "_day"
#: How many days the spine can hold, the number of rows the digit cross join produces.
MAX_SPINE_DAYS = 100_000
_DIGITS = 5
_N = "n"
_LO = "_lo"
_HI = "_hi"


def digits_select() -> exp.Expression:
    """``SELECT 0 AS n UNION ALL ... SELECT 9``, the rows the spine's numbers are built from."""
    selects = [exp.Select().select(_alias(exp.Literal.number(d), _N)) for d in range(10)]
    union: exp.Expression = selects[0]
    for select in selects[1:]:
        union = exp.union(union, select, distinct=False)
    return union


def bounds_select(
    daily: str,
    cap: exp.Expression,
    reach: Callable[[exp.Expression], exp.Expression] | None = None,
) -> exp.Select:
    """The first day of ``daily`` and the last day a window can still reach, capped at ``cap``.

    ``reach`` maps the last day with data to the last day whose frame can still contain it: six
    days later for a seven-day window, the end of the month for month to date. Without it the
    last day with data is the end. The cap keeps a far-future sentinel date out of the spine.
    Rows after it still count in every window that reaches them, they are only never shown.
    """
    day = cast("exp.Expression", exp.column(DAY_COLUMN, table=daily))
    last = cast("exp.Expression", exp.Max(this=day.copy()))
    if reach is not None:
        last = reach(last)
    capped = exp.Case(
        ifs=[exp.If(this=exp.GT(this=last, expression=cap), true=cap.copy())],
        default=last.copy(),
    )
    return (
        exp.Select()
        .select(_alias(cast("exp.Expression", exp.Min(this=day)), _LO), _alias(capped, _HI))
        .from_(exp.to_table(daily))
    )


def spine_select(bounds: str, digits: str) -> exp.Select:
    """Every day from the bounds' first day to their last, at most :data:`MAX_SPINE_DAYS`."""
    adapter = _active_adapter()
    tables = [f"d{i}" for i in range(_DIGITS)]
    number: exp.Expression = exp.column(_N, table=tables[0])
    for power, table in enumerate(tables[1:], start=1):
        number = exp.Add(
            this=number,
            expression=exp.Mul(
                this=exp.Literal.number(10**power), expression=exp.column(_N, table=table)
            ),
        )
    numbers = exp.Select().select(_alias(number, _N)).from_(_alias(exp.to_table(digits), tables[0]))
    for table in tables[1:]:
        numbers = numbers.join(_alias(exp.to_table(digits), table), join_type="CROSS")

    lo = cast("exp.Expression", exp.column(_LO, table=bounds))
    hi = cast("exp.Expression", exp.column(_HI, table=bounds))
    earliest = adapter.date_add(hi.copy(), -(MAX_SPINE_DAYS - 1), DurationUnit.DAY)
    start = exp.Case(
        ifs=[exp.If(this=exp.LT(this=lo, expression=earliest), true=earliest.copy())],
        default=lo.copy(),
    )
    window = (
        exp.Select()
        .select(_alias(start, _LO), _alias(hi.copy(), _HI))
        .from_(exp.to_table(bounds))
        .subquery("r")
    )
    day = adapter.date_add(
        cast("exp.Expression", exp.column(_LO, table="r")),
        cast("exp.Expression", exp.column(_N, table="s")),
        DurationUnit.DAY,
    )
    return (
        exp.Select()
        .select(_alias(day, DAY_COLUMN))
        .from_(window)
        .join(numbers.subquery("s"), join_type="CROSS")
        .where(exp.LTE(this=day.copy(), expression=exp.column(_HI, table="r")))
    )
