"""Serve-time staleness policy for source freshness (SPEC-E14 §3, §13).

A source's ``meta.last_validated_at`` records when ingest last checked it against the live
system. The compiler never reads the clock, so it always reports ``stale=False`` and this
module applies the project's policy afterwards, in the serving layer. With no policy
(``trust.stale_after_days`` unset, the default) nothing is ever stale, so the freshness
signal stays inactive. A source that was never validated has no age to judge and is not
stale either: absence of a stamp is not evidence of age.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult, SourceFreshness

__all__ = ["apply_staleness_policy", "is_stale"]


def is_stale(
    last_validated_at: str | None, stale_after_days: int | None, now: datetime | None = None
) -> bool:
    """Whether a validation stamp (ISO-8601) is older than the policy allows."""
    if stale_after_days is None or last_validated_at is None:
        return False
    try:
        stamp = datetime.fromisoformat(last_validated_at)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - stamp > timedelta(days=stale_after_days)


def apply_staleness_policy(
    compiled: CompileResult, stale_after_days: int | None, now: datetime | None = None
) -> CompileResult:
    """``compiled`` with ``stale`` set on every source older than ``stale_after_days``."""
    if stale_after_days is None or not compiled.freshness:
        return compiled
    freshness: list[SourceFreshness] = [
        dataclasses.replace(f, stale=is_stale(f.last_validated_at, stale_after_days, now))
        for f in compiled.freshness
    ]
    return dataclasses.replace(compiled, freshness=freshness)
