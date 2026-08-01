"""Tests for the individual trust signals (SPEC-E14 §3 signal table, §4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from canonic.compiler.result import SourceFreshness, TrustInput
from canonic.feedback.assertion_history import AssertionHistory, AssertionRecord
from canonic.feedback.history import BindingOutcomeHistory
from canonic.instrumentation.models import AnswerEvent, AnswerOutcomeEvent
from canonic.semantic.models import Provenance
from canonic.trust.models import TrustTier
from canonic.trust.signals import (
    assertion_signal,
    finality_signal,
    freshness_signal,
    outcome_signal,
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
    """S3 AC1 + SPEC-E14 §5 "+ E16 Phase 2": untested stays provisional; a benchmarked,
    passing metric can reach trusted; a failing one is capped at caution."""

    def test_no_assertion_caps_provisional_with_untested_reason(self) -> None:
        verdict = assertion_signal(
            TrustInput(metric="m", provenance="human_curated", has_assertion=False)
        )
        assert verdict.cap is TrustTier.PROVISIONAL
        assert "untested" in (verdict.reason or "")

    def test_assertion_present_no_history_caps_provisional_unverified(self) -> None:
        """No AssertionHistory supplied — same behavior as before E16 Phase 2."""
        verdict = assertion_signal(
            TrustInput(metric="m", provenance="human_curated", has_assertion=True)
        )
        assert verdict.cap is TrustTier.PROVISIONAL
        assert "unverified" in (verdict.reason or "")

    def test_assertion_present_binding_not_in_history_caps_provisional_unverified(self) -> None:
        """Assertion authored, but this binding was never harnessed (or dropped since)."""
        history = AssertionHistory(
            {"orders.order_count": AssertionRecord(ts=_ts(0), assertion_id="oc", passed=True)}
        )
        verdict = assertion_signal(
            TrustInput(
                metric="revenue",
                provenance="human_curated",
                has_assertion=True,
                binding="orders.total_revenue",
            ),
            history,
        )
        assert verdict.cap is TrustTier.PROVISIONAL
        assert "unverified" in (verdict.reason or "")

    def test_no_binding_caps_provisional_unverified_even_with_history(self) -> None:
        """Composite metrics have no single binding to join against (binding=None)."""
        history = AssertionHistory(
            {"orders.total_revenue": AssertionRecord(ts=_ts(0), assertion_id="r", passed=True)}
        )
        verdict = assertion_signal(
            TrustInput(metric="margin", provenance="human_curated", has_assertion=True), history
        )
        assert verdict.cap is TrustTier.PROVISIONAL
        assert "unverified" in (verdict.reason or "")

    def test_recorded_passing_verdict_is_inactive(self) -> None:
        """A benchmarked, passing metric's assertion signal no longer caps it — trusted
        is reachable if nothing else caps the tier (SPEC-E14 §5)."""
        history = AssertionHistory(
            {"orders.total_revenue": AssertionRecord(ts=_ts(0), assertion_id="r", passed=True)}
        )
        verdict = assertion_signal(
            TrustInput(
                metric="revenue",
                provenance="human_curated",
                has_assertion=True,
                binding="orders.total_revenue",
            ),
            history,
        )
        assert verdict.cap is None

    def test_recorded_failing_verdict_caps_caution(self) -> None:
        """SPEC-E14 §7 AC1: a failing assertion caps the tier at caution."""
        history = AssertionHistory(
            {"orders.total_revenue": AssertionRecord(ts=_ts(0), assertion_id="r", passed=False)}
        )
        verdict = assertion_signal(
            TrustInput(
                metric="revenue",
                provenance="human_curated",
                has_assertion=True,
                binding="orders.total_revenue",
            ),
            history,
        )
        assert verdict.cap is TrustTier.CAUTION
        assert "failed" in (verdict.reason or "")
        assert "r" in (verdict.reason or "")


class TestStaticSignalsFor:
    def test_builds_two_signals_per_metric(self) -> None:
        inputs = [
            TrustInput(metric="a", provenance="human_curated", has_assertion=True),
            TrustInput(metric="b", provenance="inferred", has_assertion=False),
        ]
        signals = static_signals_for(inputs)
        assert len(signals) == 4

    def test_threads_assertion_history_through_to_each_metric(self) -> None:
        history = AssertionHistory(
            {"orders.total_revenue": AssertionRecord(ts=_ts(0), assertion_id="r", passed=True)}
        )
        inputs = [
            TrustInput(
                metric="revenue",
                provenance="human_curated",
                has_assertion=True,
                binding="orders.total_revenue",
            )
        ]
        signals = static_signals_for(inputs, history)
        # provenance (inactive, human_curated) + assertion (inactive, recorded pass).
        assert all(s.cap is None for s in signals)


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


_BASE_ANSWER: dict[str, Any] = {
    "ts": "2026-01-01T00:00:00+00:00",
    "kind": "served_answer",
    "contract_schema": "2.2",
    "query_hash": "sha256:aaa",
    "compiled_sql_hash": "sha256:bbb",
    "connection": "wh",
    "resolved": {"metrics": {"revenue": "orders.total_revenue"}},
    "guardrails_fired": [],
    "finality": None,
    "freshness": [],
    "latency_ms": 100,
    "bytes_scanned": None,
    "error": None,
    "trust_score": None,
    "cache_hit": None,
    "over_limit_blocked": None,
}

_BASE_OUTCOME: dict[str, Any] = {
    "ts": "2026-01-01T00:01:00+00:00",
    "kind": "answer_outcome",
    "ref": "sha256:aaa",
    "verdict": "incorrect",
    "reason_code": "wrong_definition",
    "correction": None,
    "marked_by": "analyst",
}


def _ts(days_ago: float = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _history(**outcome_overrides: Any) -> BindingOutcomeHistory:
    answer = AnswerEvent.model_validate(_BASE_ANSWER)
    outcome = AnswerOutcomeEvent.model_validate(
        {**_BASE_OUTCOME, "ts": _ts(1), **outcome_overrides}
    )
    return BindingOutcomeHistory.from_events([answer], [outcome])


class TestOutcomeSignal:
    """SPEC-E11 §5: a recent confirmed-wrong_definition caps the binding at caution."""

    def test_capped_binding_caps_caution(self) -> None:
        trust_input = TrustInput(
            metric="revenue",
            provenance="human_curated",
            has_assertion=True,
            binding="orders.total_revenue",
        )
        verdict = outcome_signal(trust_input, _history(), window_days=90)
        assert verdict.cap is TrustTier.CAUTION
        assert verdict.reason == "outcome: confirmed-wrong"

    def test_no_binding_is_inactive(self) -> None:
        trust_input = TrustInput(
            metric="conversion_rate", provenance="human_curated", has_assertion=True
        )
        verdict = outcome_signal(trust_input, _history(), window_days=90)
        assert verdict.cap is None

    def test_no_history_for_this_binding_is_inactive(self) -> None:
        trust_input = TrustInput(
            metric="order_count",
            provenance="human_curated",
            has_assertion=True,
            binding="orders.order_count",
        )
        verdict = outcome_signal(trust_input, _history(), window_days=90)
        assert verdict.cap is None

    def test_wrong_data_never_caps(self) -> None:
        trust_input = TrustInput(
            metric="revenue",
            provenance="human_curated",
            has_assertion=True,
            binding="orders.total_revenue",
        )
        verdict = outcome_signal(trust_input, _history(reason_code="wrong_data"), window_days=90)
        assert verdict.cap is None
