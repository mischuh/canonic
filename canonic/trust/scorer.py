"""Worst-signal-dominates aggregation (SPEC-E14 §3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.trust.models import SignalVerdict, TrustScore, TrustTier
from canonic.trust.signals import finality_signal, freshness_signal, static_signals_for

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canonic.compiler.result import CompileResult
    from canonic.connectors.base import ResultSet

__all__ = ["TrustScorer", "trust_for_compiled"]

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


def trust_for_compiled(compiled: CompileResult, result: ResultSet | None = None) -> TrustScore:
    """Compute the trust tier for a compiled query (SPEC-E14 §3, §6).

    Shared by the served ``QueryMetadata.trust_score`` block and the E16 ``AnswerEvent``
    log (SPEC-E16 Part 2 §4) so both surfaces score trust identically. Row-level finality
    tallies require ``result``; when it's absent (e.g. logging a failed query) the
    finality signal stays inactive rather than guessing.
    """
    final_rows: int | None = None
    provisional_rows: int | None = None
    if compiled.finality is not None and result is not None:
        col_names = [c.name for c in result.columns]
        if "is_final" in col_names:
            idx = col_names.index("is_final")
            final_rows = sum(1 for row in result.rows if row[idx])
            provisional_rows = len(result.rows) - final_rows
    return TrustScorer.score(
        [
            *static_signals_for(compiled.trust_inputs),
            finality_signal(final_rows, provisional_rows),
            freshness_signal(compiled.freshness),
        ]
    )
