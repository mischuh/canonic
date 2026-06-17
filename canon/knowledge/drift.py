"""Drift & freshness detection for knowledge pages (SPEC-E6 §7).

Two trust signals, both read-side and side-effect-free:

- **Drift review flag.** ``meta.bound_fingerprints`` records the measure-definition
  fingerprint each page depends on. When a bound measure's ``expr`` changes, the rendered
  ``{{ sl:….expr }}`` auto-updates (:mod:`canon.knowledge.rendering`), but the surrounding
  *prose* may no longer be accurate — so the page is flagged for review (S6 AC2). The flag
  is a signal, not a silent edit; resolution flows through E4's diff/review.
- **Staleness signal.** ``meta.last_validated_at`` records when a page's references were last
  checked against live entities. Serving (E7/E8) surfaces a :class:`StalenessSignal` at query
  time so the agent can caveat truthfully (S8). Trust is not binary.

:class:`DriftDetector` is a pure advisor — it computes signals and writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta  # noqa: TC003 — runtime use in signatures
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from canon.knowledge.models import KnowledgePage
    from canon.knowledge.validation import EntityIndex

__all__ = [
    "DriftDetector",
    "StalenessSignal",
]


class StalenessSignal(BaseModel):
    """A query-time freshness signal derived from ``meta.last_validated_at`` (SPEC-E6 §8).

    ``age_days`` is the whole days since the page's references were last validated, or
    ``None`` when the page was never validated. ``message`` is a ready-to-surface caveat.
    """

    model_config = ConfigDict(frozen=True)

    page: str
    last_validated_at: datetime | None
    age_days: int | None  # None ⇒ never validated
    message: str


class DriftDetector:
    """Computes drift review flags and staleness signals for a page (SPEC-E6 §7)."""

    def flagged_for_review(self, page: KnowledgePage, entity_index: EntityIndex) -> list[str]:
        """Return the bound entity names whose live fingerprint differs from the recorded one.

        Compares ``page.meta.bound_fingerprints`` against
        :meth:`~canon.knowledge.validation.EntityIndex.current_fingerprint`. A name whose
        measure has disappeared yields no live fingerprint and is skipped — that is pruning's
        concern (SPEC-E6 §3.2), not a prose-review flag. Names are returned sorted for
        deterministic output.
        """
        flagged = [
            name
            for name, bound_fp in page.meta.bound_fingerprints.items()
            if (current := entity_index.current_fingerprint(name)) is not None
            and current != bound_fp
        ]
        return sorted(flagged)

    def staleness(
        self,
        page: KnowledgePage,
        *,
        window: timedelta,
        now: datetime | None = None,
    ) -> StalenessSignal | None:
        """Return a :class:`StalenessSignal` when ``page`` is stale, else ``None``.

        A page is stale when it was never validated (``last_validated_at is None``) or was
        last validated more than ``window`` ago. ``now`` defaults to the current UTC time;
        it is injectable for deterministic tests.
        """
        now = now or datetime.now(UTC)
        last = page.meta.last_validated_at
        if last is None:
            return StalenessSignal(
                page=page.id,
                last_validated_at=None,
                age_days=None,
                message="referenced definitions never validated",
            )
        age = now - last
        if age <= window:
            return None
        age_days = age.days
        return StalenessSignal(
            page=page.id,
            last_validated_at=last,
            age_days=age_days,
            message=f"referenced definitions unvalidated for {age_days} days",
        )
