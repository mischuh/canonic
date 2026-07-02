"""Watermark evaluation and finality-rule validation (SPEC-Fuller-E15 §2, §5.1).

The two public-facing functions are the only place watermark strings are interpreted:
  - ``evaluate_watermark`` is called by the compiler at stage 5 (deterministic when
    ``as_of`` is supplied; wall-clock otherwise — the only non-deterministic path).
  - ``validate_finality_rule`` is called by the contract validator and by the
    ``FinalityRule`` model validator before any diff is emitted (§5.1).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from canonic.contracts.models import FinalityRule

__all__ = ["evaluate_watermark", "validate_finality_rule"]

# Grammar: "business_day" optionally followed by "+ N day[s]" or "- N day[s]".
# "business_day" alone means the current business day (T+0); "- 1 day" means T-1.
_WATERMARK_RE = re.compile(
    r"^\s*business_day"
    r"(?:\s*(?P<sign>[+\-])\s*(?P<n>\d+)\s*days?)?"
    r"\s*$",
    re.IGNORECASE,
)


def _resolve_tz(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise ValueError(f"unknown timezone {tz!r}") from exc


def _prev_business_day(dt: datetime) -> datetime:
    """Return the nearest *previous* business day (Mon–Fri, same calendar day if already one)."""
    from datetime import timedelta

    d = dt.date()
    # weekday(): Mon=0 … Sun=6
    if d.weekday() == 5:  # Saturday → Friday
        d -= timedelta(days=1)
    elif d.weekday() == 6:  # Sunday → Friday
        d -= timedelta(days=2)
    return datetime.combine(d, time.min).replace(tzinfo=dt.tzinfo)


def evaluate_watermark(expr: str, tz: str, as_of: datetime | None) -> datetime:
    """Evaluate a watermark time expression to a tz-aware end-of-business datetime.

    The result is the **end of the resolved business day** (23:59:59) in ``tz``,
    so rows with ``created_at <= watermark`` fall on or before that day.

    Args:
        expr: e.g. ``"business_day - 1 day"``.
        tz:   IANA timezone string, e.g. ``"America/New_York"``.
        as_of: reference point; ``None`` → ``datetime.now(tz)``.

    Returns:
        A tz-aware datetime at 23:59:59 on the resolved business day.

    Raises:
        ValueError: if ``expr`` does not match the supported grammar or ``tz`` is unknown.
    """
    zone = _resolve_tz(tz)
    m = _WATERMARK_RE.match(expr)
    if not m:
        raise ValueError(
            f"unsupported watermark expression {expr!r}; "
            "expected 'business_day' optionally followed by '± N day[s]'"
        )
    base = as_of.astimezone(zone) if as_of is not None else datetime.now(zone)
    base_bd = _prev_business_day(base)

    offset_days = 0
    if m.group("n") is not None:
        sign = 1 if m.group("sign") == "+" else -1
        offset_days = sign * int(m.group("n"))

    from datetime import timedelta

    target = base_bd + timedelta(days=offset_days)
    # Re-apply prev-business-day in case the offset landed on a weekend.
    target = _prev_business_day(target)
    return target.replace(hour=23, minute=59, second=59, microsecond=0)


def validate_finality_rule(rule: FinalityRule, source_names: set[str] | None = None) -> None:
    """Structural + cross-surface validation for a finality rule (SPEC §5.1).

    Checks:
    - Exactly one realization carries ``role == "final"``.
    - Every ``role`` is ``"final"`` or ``"provisional"``.
    - The ``final`` realization has a non-empty ``watermark`` that parses.
    - The ``final`` realization has a non-empty, valid ``tz``.
    - When ``source_names`` is supplied, every realization source is present.

    Raises:
        ValueError: on any structural violation (message includes the problematic value).
    """
    valid_roles = {"final", "provisional"}
    final_realizations = []
    for r in rule.realizations:
        if r.role not in valid_roles:
            raise ValueError(
                f"realization for source {r.source!r} has unknown role {r.role!r}; "
                f"expected 'final' or 'provisional'"
            )
        if r.role == "final":
            final_realizations.append(r)
        if source_names is not None and r.source not in source_names:
            raise ValueError(f"realization source {r.source!r} is not declared in semantics")

    if len(final_realizations) != 1:
        raise ValueError(
            f"finality rule for metric {rule.metric!r} must have exactly one 'final' "
            f"realization; found {len(final_realizations)}"
        )

    final = final_realizations[0]
    if not final.watermark:
        raise ValueError(
            f"'final' realization (source {final.source!r}) requires a 'watermark' expression"
        )
    if not final.tz:
        raise ValueError(f"'final' realization (source {final.source!r}) requires a 'tz' value")
    try:
        _resolve_tz(final.tz)
    except ValueError:
        raise
    try:
        _WATERMARK_RE_check = _WATERMARK_RE.match(final.watermark)
        if not _WATERMARK_RE_check:
            raise ValueError(
                f"watermark expression {final.watermark!r} does not match the supported grammar; "
                "expected 'business_day' optionally followed by '± N day[s]'"
            )
    except ValueError:
        raise

    # Validate coalescing rule references declared roles.
    if rule.coalescing is not None:
        declared_roles = {r.role for r in rule.realizations}
        for role in ("final", "provisional"):
            if role in rule.coalescing and role not in declared_roles:
                raise ValueError(
                    f"coalescing rule references role {role!r} but no realization declares it"
                )


def watermark_to_iso(dt: datetime) -> str:
    """Serialize a watermark datetime to the ISO-8601 string used in result metadata."""
    # Emit offset form (e.g. "-04:00") rather than "UTC" — matches spec §2.3 example.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat(timespec="seconds")
