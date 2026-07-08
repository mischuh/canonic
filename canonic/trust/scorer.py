"""Worst-signal-dominates aggregation (SPEC-E14 §3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.trust.models import SignalVerdict, TrustScore, TrustTier

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["TrustScorer"]

_TIER_ORDER = list(TrustTier)


class TrustScorer:
    """Computes a :class:`TrustScore` from independent signal verdicts.

    The tier is the lowest cap any active signal forces (worst-signal-dominates): a
    single ``caution`` verdict caps the whole answer at ``caution`` even if every other
    signal is clean. ``reasons`` lists exactly the signals that capped the final tier.
    Given the same signals, the result is identical every time — a pure function, off
    the compiler's SQL-generation path (SPEC-E14 §3, S6).
    """

    @staticmethod
    def score(signals: Iterable[SignalVerdict]) -> TrustScore:
        worst = TrustTier.TRUSTED
        reasons: list[str] = []
        for verdict in signals:
            if verdict.cap is None:
                continue
            if _TIER_ORDER.index(verdict.cap) < _TIER_ORDER.index(worst):
                worst = verdict.cap
                reasons = [verdict.reason] if verdict.reason else []
            elif verdict.cap is worst and verdict.reason:
                reasons.append(verdict.reason)
        return TrustScore(tier=worst, reasons=tuple(reasons))
