"""Tests for the individual trust signals (SPEC-E14 §3 signal table, §4)."""

from __future__ import annotations

from canonic.compiler.result import SourceFreshness, TrustInput
from canonic.semantic.models import Provenance
from canonic.trust.models import TrustTier
from canonic.trust.signals import (
    assertion_signal,
    finality_signal,
    freshness_signal,
    provenance_signal,
    static_signals_for,
)


class TestProvenanceSignal:
    def test_inferred_caps_provisional(self) -> None:
        verdict = provenance_signal(
            TrustInput(metric="m", provenance=Provenance.INFERRED.value, has_assertion=False)
        )
        assert verdict.cap is TrustTier.PROVISIONAL
        assert verdict.reason == "m: binding inferred"

    def test_human_curated_is_inactive(self) -> None:
        verdict = provenance_signal(
            TrustInput(metric="m", provenance=Provenance.HUMAN_CURATED.value, has_assertion=False)
        )
        assert verdict.cap is None

    def test_board_approved_is_inactive(self) -> None:
        verdict = provenance_signal(
            TrustInput(metric="m", provenance=Provenance.BOARD_APPROVED.value, has_assertion=False)
        )
        assert verdict.cap is None


class TestAssertionSignal:
    """S3 AC1: an untested metric is provisional, never trusted."""

    def test_no_assertion_caps_provisional_with_untested_reason(self) -> None:
        verdict = assertion_signal(
            TrustInput(metric="m", provenance="human_curated", has_assertion=False)
        )
        assert verdict.cap is TrustTier.PROVISIONAL
        assert "untested" in (verdict.reason or "")

    def test_assertion_present_still_caps_provisional_pending_e16(self) -> None:
        """v1 has no pass/fail harness (E16); an authored assertion cannot yet earn trusted."""
        verdict = assertion_signal(
            TrustInput(metric="m", provenance="human_curated", has_assertion=True)
        )
        assert verdict.cap is TrustTier.PROVISIONAL
        assert "unverified" in (verdict.reason or "")


class TestStaticSignalsFor:
    def test_builds_two_signals_per_metric(self) -> None:
        inputs = [
            TrustInput(metric="a", provenance="human_curated", has_assertion=True),
            TrustInput(metric="b", provenance="inferred", has_assertion=False),
        ]
        signals = static_signals_for(inputs)
        assert len(signals) == 4


class TestFinalitySignal:
    def test_no_provisional_rows_is_inactive(self) -> None:
        assert finality_signal(final_rows=10, provisional_rows=0).cap is None
        assert finality_signal(final_rows=None, provisional_rows=None).cap is None

    def test_provisional_rows_present_caps_provisional(self) -> None:
        verdict = finality_signal(final_rows=5, provisional_rows=2)
        assert verdict.cap is TrustTier.PROVISIONAL
        assert verdict.reason is not None


class TestFreshnessSignal:
    def test_no_stale_sources_is_inactive(self) -> None:
        fresh = [SourceFreshness(source="orders", last_validated_at=None, stale=False)]
        assert freshness_signal(fresh).cap is None

    def test_stale_source_caps_provisional(self) -> None:
        stale = [SourceFreshness(source="orders", last_validated_at=None, stale=True)]
        verdict = freshness_signal(stale)
        assert verdict.cap is TrustTier.PROVISIONAL
        assert "orders" in (verdict.reason or "")

    def test_empty_freshness_list_is_inactive(self) -> None:
        assert freshness_signal([]).cap is None
