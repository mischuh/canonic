"""Unit tests for drift review flags and staleness signals (SPEC-E6 §7, S6 AC2 / S8 AC1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from canon.knowledge.drift import DriftDetector
from canon.knowledge.models import KnowledgePageMeta
from canon.semantic.models import Measure, compute_measure_fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable

    from canon.knowledge.models import KnowledgePage
    from canon.knowledge.validation import EntityIndex

_REVENUE = "warehouse_pg.orders.total_revenue"
_LIVE_FP = compute_measure_fingerprint(Measure(name="total_revenue", expr="sum(amount)"))


def test_matching_fingerprint_not_flagged(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A bound fingerprint equal to the live one raises no review flag (S6 AC2)."""
    page = make_page(meta=KnowledgePageMeta(bound_fingerprints={_REVENUE: _LIVE_FP}))

    assert DriftDetector().flagged_for_review(page, entity_index) == []


def test_changed_expr_flags_for_review(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A stale bound fingerprint flags the page for prose review (S6 AC2)."""
    page = make_page(meta=KnowledgePageMeta(bound_fingerprints={_REVENUE: "sha256:stale"}))

    assert DriftDetector().flagged_for_review(page, entity_index) == [_REVENUE]


def test_disappeared_entity_not_flagged(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A bound entity that no longer exists is pruning's job, not a review flag."""
    gone = "warehouse_pg.orders.gone"
    page = make_page(meta=KnowledgePageMeta(bound_fingerprints={gone: "sha256:whatever"}))

    assert DriftDetector().flagged_for_review(page, entity_index) == []


def test_staleness_beyond_window(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A page validated beyond the window yields a signal with positive age (S8 AC1)."""
    now = datetime(2026, 6, 17, tzinfo=UTC)
    validated = datetime(2026, 3, 1, tzinfo=UTC)
    page = make_page(meta=KnowledgePageMeta(last_validated_at=validated))

    signal = DriftDetector().staleness(page, window=timedelta(days=90), now=now)

    assert signal is not None
    assert signal.page == page.id
    assert signal.age_days == (now - validated).days
    assert "unvalidated" in signal.message


def test_staleness_within_window_is_none(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A recently validated page yields no staleness signal (S8 AC1)."""
    now = datetime(2026, 6, 17, tzinfo=UTC)
    page = make_page(meta=KnowledgePageMeta(last_validated_at=datetime(2026, 6, 1, tzinfo=UTC)))

    assert DriftDetector().staleness(page, window=timedelta(days=90), now=now) is None


def test_never_validated_yields_signal(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A page that was never validated is stale with age_days=None (S8 AC1)."""
    page = make_page(meta=KnowledgePageMeta(last_validated_at=None))

    signal = DriftDetector().staleness(page, window=timedelta(days=90))

    assert signal is not None
    assert signal.age_days is None
    assert signal.last_validated_at is None
    assert "never validated" in signal.message
