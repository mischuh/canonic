"""Tests for QueryMetadata.trust_score assembly (SPEC-E14 §6, S1, S4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from canonic.compiler.result import CompileResult, TrustInput
from canonic.core.models import QueryMetadata
from canonic.feedback.history import BindingOutcomeHistory
from canonic.instrumentation.models import AnswerEvent, AnswerOutcomeEvent


def _make_compile_result(trust_inputs: list[TrustInput]) -> CompileResult:
    return CompileResult(
        sql="SELECT 1",
        dialect="postgres",
        resolved={"revenue": "orders.total_revenue"},
        trust_inputs=trust_inputs,
    )


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


class TestTrustScoreAssembly:
    def test_untested_metric_is_provisional_with_reasons(self) -> None:
        """S1 AC1 / S3 AC1: a below-trusted tier always carries a non-empty reasons list."""
        compiled = _make_compile_result(
            [TrustInput(metric="revenue", provenance="human_curated", has_assertion=False)]
        )
        meta = QueryMetadata.from_compile_result(compiled)
        assert meta.trust_score is not None
        assert meta.trust_score.tier == "provisional"
        assert meta.trust_score.reasons

    def test_inferred_binding_adds_a_reason(self) -> None:
        compiled = _make_compile_result(
            [TrustInput(metric="revenue", provenance="inferred", has_assertion=False)]
        )
        meta = QueryMetadata.from_compile_result(compiled)
        assert meta.trust_score is not None
        assert any("inferred" in r for r in meta.trust_score.reasons)

    def test_no_trust_inputs_does_not_error(self) -> None:
        """S4 AC1: graceful degradation — no active signal, no failure."""
        compiled = _make_compile_result([])
        meta = QueryMetadata.from_compile_result(compiled)
        assert meta.trust_score is not None
        assert meta.trust_score.tier == "trusted"
        assert meta.trust_score.reasons == []


class TestOutcomeHistorySignal:
    """SPEC-E11 §5: a recent confirmed-wrong_definition caps served trust at caution."""

    def test_capped_binding_forces_caution(self) -> None:
        compiled = _make_compile_result(
            [
                TrustInput(
                    metric="revenue",
                    provenance="human_curated",
                    has_assertion=True,
                    binding="orders.total_revenue",
                )
            ]
        )
        meta = QueryMetadata.from_compile_result(
            compiled, outcome_history=_history(), outcome_window_days=90
        )
        assert meta.trust_score is not None
        assert meta.trust_score.tier == "caution"
        assert "outcome: confirmed-wrong" in meta.trust_score.reasons

    def test_no_outcome_history_leaves_scoring_static_only(self) -> None:
        """Omitting outcome_history is fully backward compatible — static scoring unchanged."""
        compiled = _make_compile_result(
            [
                TrustInput(
                    metric="revenue",
                    provenance="human_curated",
                    has_assertion=True,
                    binding="orders.total_revenue",
                )
            ]
        )
        meta = QueryMetadata.from_compile_result(compiled)
        assert meta.trust_score is not None
        assert meta.trust_score.tier == "provisional"

    def test_wrong_data_outcome_does_not_cap(self) -> None:
        """S1/S4-AC2: only wrong_definition ever caps trust."""
        compiled = _make_compile_result(
            [
                TrustInput(
                    metric="revenue",
                    provenance="human_curated",
                    has_assertion=True,
                    binding="orders.total_revenue",
                )
            ]
        )
        meta = QueryMetadata.from_compile_result(
            compiled,
            outcome_history=_history(reason_code="wrong_data"),
            outcome_window_days=90,
        )
        assert meta.trust_score is not None
        assert meta.trust_score.tier == "provisional"
        assert "outcome: confirmed-wrong" not in meta.trust_score.reasons

    def test_composite_metric_with_no_binding_is_unaffected(self) -> None:
        """A ratio/weighted_avg TrustInput carries no physical binding — signal stays inactive."""
        compiled = _make_compile_result(
            [TrustInput(metric="conversion_rate", provenance="human_curated", has_assertion=True)]
        )
        meta = QueryMetadata.from_compile_result(
            compiled, outcome_history=_history(), outcome_window_days=90
        )
        assert meta.trust_score is not None
        assert "outcome: confirmed-wrong" not in meta.trust_score.reasons
