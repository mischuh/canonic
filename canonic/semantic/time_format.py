"""The ``time_format`` of a label dimension: how its values spell the period they stand for.

A label dimension (``time_grain``) holds text like ``2026-43`` or ``202610`` rather than a date.
The serving layer needs the period's start to tell whether it has ended, and ``decompose``
needs it for the open-period check. ``time_format`` names the form with ``strftime``-style
placeholders, so canonic reads the labels exactly as the project writes them instead of
guessing.

Only fixed-width numeric placeholders are supported, so a format reads one way only and is
the same in every warehouse dialect.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from canonic.exc import InvalidTimeFormat

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["DIRECTIVES", "REQUIRED_DIRECTIVES", "TimeFormat", "compile_time_format"]

#: Each supported placeholder and the pattern of the digits it reads.
DIRECTIVES: dict[str, str] = {
    "Y": r"\d{4}",  # calendar year
    "G": r"\d{4}",  # ISO 8601 week-numbering year
    "q": r"[1-4]",  # quarter, 1 to 4
    "m": r"\d{2}",  # month, 01 to 12
    "V": r"\d{2}",  # ISO 8601 week, 01 to 53
    "d": r"\d{2}",  # day of the month, 01 to 31
    "H": r"\d{2}",  # hour, 00 to 23
}

#: The placeholders a format of each grain must contain, most significant first.
REQUIRED_DIRECTIVES: dict[str, tuple[str, ...]] = {
    "year": ("Y",),
    "quarter": ("Y", "q"),
    "month": ("Y", "m"),
    "week": ("G", "V"),
    "day": ("Y", "m", "d"),
    "hour": ("Y", "m", "d", "H"),
}

_TOKEN = re.compile(r"%(.)")


@dataclass(frozen=True, slots=True)
class TimeFormat:
    """A validated ``time_format`` for one grain.

    Construct it with :meth:`compile`, which rejects an unknown placeholder, one used twice,
    and a set of placeholders that does not match the grain.
    """

    format: str
    grain: str
    directives: tuple[str, ...]
    pattern: re.Pattern[str]

    @classmethod
    def compile(cls, format: str, grain: str) -> TimeFormat:  # noqa: A002 — the field's name
        """Parse ``format`` for ``grain`` or raise :class:`~canonic.exc.InvalidTimeFormat`."""
        directives: list[str] = []
        regex: list[str] = []
        position = 0
        for match in _TOKEN.finditer(format):
            regex.append(re.escape(format[position : match.start()]))
            position = match.end()
            directive = match[1]
            if directive == "%":
                regex.append("%")
                continue
            if directive not in DIRECTIVES:
                raise InvalidTimeFormat(
                    f"time_format {format!r} uses the unsupported placeholder %{directive}; "
                    f"use {_placeholders(DIRECTIVES)}"
                )
            if directive in directives:
                raise InvalidTimeFormat(f"time_format {format!r} uses %{directive} twice")
            directives.append(directive)
            regex.append(f"(?P<{directive}>{DIRECTIVES[directive]})")
        if "%" in format[position:]:
            raise InvalidTimeFormat(f"time_format {format!r} ends with a lone '%'")
        regex.append(re.escape(format[position:]))
        required = REQUIRED_DIRECTIVES[grain]
        if set(directives) != set(required):
            raise InvalidTimeFormat(
                f"time_format {format!r} does not fit time_grain {grain!r}: a {grain} label "
                f"needs exactly {_placeholders(required)}"
            )
        return cls(format, grain, tuple(directives), re.compile("".join(regex)))

    @property
    def sorts_chronologically(self) -> bool:
        """Whether labels in this format sort as text in the order of their periods.

        True when the placeholders run from most to least significant. Every placeholder has a
        fixed width, so the literals between them line up and text order is time order.
        """
        return self.directives == REQUIRED_DIRECTIVES[self.grain]

    def parse(self, text: str) -> datetime | None:
        """The start of the period ``text`` stands for, or ``None`` if it does not fit.

        A week starts on its Monday, as in ISO 8601.
        """
        match = self.pattern.fullmatch(text.strip())
        if match is None:
            return None
        parts = {directive: int(match[directive]) for directive in self.directives}
        try:
            if self.grain == "week":
                monday = date.fromisocalendar(parts["G"], parts["V"], 1)
                return datetime(monday.year, monday.month, monday.day)
            month = 3 * (parts["q"] - 1) + 1 if "q" in parts else parts.get("m", 1)
            return datetime(parts["Y"], month, parts.get("d", 1), parts.get("H", 0))
        except ValueError:
            return None

    def render(self, start: datetime) -> str:
        """The label of the period that begins at ``start``, for messages and examples."""
        iso = start.isocalendar()
        values = {
            "Y": f"{start.year:04d}",
            "G": f"{iso.year:04d}",
            "q": str((start.month - 1) // 3 + 1),
            "m": f"{start.month:02d}",
            "V": f"{iso.week:02d}",
            "d": f"{start.day:02d}",
            "H": f"{start.hour:02d}",
            "%": "%",
        }
        return _TOKEN.sub(lambda match: values[match[1]], self.format)


@functools.lru_cache(maxsize=256)
def compile_time_format(format: str, grain: str) -> TimeFormat:  # noqa: A002 — the field's name
    """:meth:`TimeFormat.compile`, cached: the serving layer reads every row with it."""
    return TimeFormat.compile(format, grain)


def _placeholders(directives: Iterable[str]) -> str:
    return ", ".join(f"%{directive}" for directive in directives)
