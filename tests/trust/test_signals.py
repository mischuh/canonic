"""Tests for the individual trust signals (SPEC-E14 §3 signal table, §4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from canonic.compiler.result import SourceFreshness, TrustInput
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
