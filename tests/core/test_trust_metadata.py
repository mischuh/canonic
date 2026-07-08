"""Tests for QueryMetadata.trust_score assembly (SPEC-E14 §6, S1, S4)."""

from __future__ import annotations

from canonic.compiler.result import CompileResult, TrustInput
from canonic.core.models import QueryMetadata


def _make_compile_result(trust_inputs: list[TrustInput]) -> CompileResult:
    return CompileResult(
        sql="SELECT 1",
        dialect="postgres",
        resolved={"revenue": "orders.total_revenue"},
        trust_inputs=trust_inputs,
    )


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
