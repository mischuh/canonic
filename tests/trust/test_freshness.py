"""The serve-time staleness policy (SPEC-E14 §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from canonic.compiler.result import CompileResult, SourceFreshness
from canonic.trust.freshness import apply_staleness_policy, is_stale

_NOW = datetime(2026, 10, 6, tzinfo=UTC)


def _stamp(days_ago: int) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _compiled(*freshness: SourceFreshness) -> CompileResult:
    return CompileResult(sql="SELECT 1", dialect="postgres", resolved={}, freshness=list(freshness))


def test_no_policy_is_never_stale() -> None:
    assert not is_stale(_stamp(10_000), None, _NOW)


def test_never_validated_source_is_not_stale() -> None:
    assert not is_stale(None, 30, _NOW)


def test_age_is_compared_with_the_window() -> None:
    assert not is_stale(_stamp(30), 30, _NOW)
    assert is_stale(_stamp(31), 30, _NOW)


def test_naive_stamp_is_read_as_utc() -> None:
    naive = (_NOW - timedelta(days=40)).replace(tzinfo=None).isoformat()
    assert is_stale(naive, 30, _NOW)


def test_unparseable_stamp_is_not_stale() -> None:
    assert not is_stale("last tuesday", 30, _NOW)


def test_policy_marks_only_the_old_sources() -> None:
    compiled = _compiled(
        SourceFreshness(source="old", last_validated_at=_stamp(90), stale=False),
        SourceFreshness(source="new", last_validated_at=_stamp(1), stale=False),
        SourceFreshness(source="unknown", last_validated_at=None, stale=False),
    )
    out = apply_staleness_policy(compiled, 30, _NOW)
    assert {f.source: f.stale for f in out.freshness} == {
        "old": True,
        "new": False,
        "unknown": False,
    }
    assert [f.stale for f in compiled.freshness] == [False, False, False]  # input untouched


def test_policy_off_returns_the_same_result() -> None:
    compiled = _compiled(SourceFreshness(source="old", last_validated_at=_stamp(90), stale=False))
    assert apply_staleness_policy(compiled, None, _NOW) is compiled
