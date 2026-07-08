"""Tests for TrustScorer — worst-signal-dominates aggregation (SPEC-E14 §3, S2, S6)."""

from __future__ import annotations

from canonic.trust.models import SignalVerdict, TrustTier
from canonic.trust.scorer import TrustScorer


class TestNoActiveSignals:
    def test_all_inactive_defaults_to_trusted_with_no_reasons(self) -> None:
        score = TrustScorer.score([SignalVerdict(cap=None), SignalVerdict(cap=None)])
        assert score.tier is TrustTier.TRUSTED
        assert score.reasons == ()

    def test_empty_signal_list_defaults_to_trusted(self) -> None:
        score = TrustScorer.score([])
        assert score.tier is TrustTier.TRUSTED
        assert score.reasons == ()


class TestWorstSignalDominates:
    """S2 AC1/AC2: the tier is the lowest cap any active signal forces."""

    def test_single_provisional_signal_caps_tier(self) -> None:
        score = TrustScorer.score(
            [SignalVerdict(cap=TrustTier.PROVISIONAL, reason="binding: inferred")]
        )
        assert score.tier is TrustTier.PROVISIONAL
        assert score.reasons == ("binding: inferred",)

    def test_caution_outranks_provisional(self) -> None:
        score = TrustScorer.score(
            [
                SignalVerdict(cap=TrustTier.PROVISIONAL, reason="binding: inferred"),
                SignalVerdict(cap=TrustTier.CAUTION, reason="assertion: failed"),
            ]
        )
        assert score.tier is TrustTier.CAUTION
        assert score.reasons == ("assertion: failed",)

    def test_order_of_signals_does_not_matter(self) -> None:
        signals = [
            SignalVerdict(cap=TrustTier.CAUTION, reason="assertion: failed"),
            SignalVerdict(cap=TrustTier.PROVISIONAL, reason="binding: inferred"),
        ]
        assert TrustScorer.score(signals).tier is TrustTier.CAUTION
        assert TrustScorer.score(list(reversed(signals))).tier is TrustTier.CAUTION

    def test_reasons_accumulate_at_the_worst_tier_only(self) -> None:
        score = TrustScorer.score(
            [
                SignalVerdict(cap=TrustTier.CAUTION, reason="drift: flagged"),
                SignalVerdict(cap=TrustTier.CAUTION, reason="outcome: confirmed-wrong"),
                SignalVerdict(cap=TrustTier.PROVISIONAL, reason="binding: inferred"),
            ]
        )
        assert score.tier is TrustTier.CAUTION
        assert score.reasons == ("drift: flagged", "outcome: confirmed-wrong")

    def test_inactive_signals_never_move_the_tier(self) -> None:
        score = TrustScorer.score(
            [
                SignalVerdict(cap=None),
                SignalVerdict(cap=TrustTier.PROVISIONAL, reason="untested"),
                SignalVerdict(cap=None),
            ]
        )
        assert score.tier is TrustTier.PROVISIONAL
        assert score.reasons == ("untested",)


class TestDeterminism:
    """S6 AC1: identical signals give identical tier and reasons every time."""

    def test_repeated_scoring_is_identical(self) -> None:
        signals = [
            SignalVerdict(cap=TrustTier.PROVISIONAL, reason="binding: inferred"),
            SignalVerdict(cap=TrustTier.PROVISIONAL, reason="untested"),
        ]
        first = TrustScorer.score(signals)
        second = TrustScorer.score(signals)
        assert first == second
