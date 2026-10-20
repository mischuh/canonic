"""Compiler stage 6b acceptance tests for the min_trust guardrail (SPEC-E14 §7 S5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    Assertion,
    AssertionExpect,
    Guardrail,
    GuardrailKind,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.exc import GuardrailBlock
from canonic.feedback.assertion_history import AssertionHistory, AssertionRecord

if TYPE_CHECKING:
    from canonic.contracts.models import MetricBinding
    from canonic.semantic.models import SemanticSource


class TestMinTrustBlocksUnmetFloor:
    """S5 AC1: a provisional answer is blocked by a min_trust: trusted guardrail."""

    def test_blocks_when_tier_below_level(
        self, min_trust_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock) as exc_info:
            compile(
                SemanticQuery(metrics=["revenue"], context="board_reporting"),
                min_trust_resolver,
                sources,
            )
        assert exc_info.value.exit_code == 8
        assert "human-approved" in str(exc_info.value)

    def test_deterministic_block(
        self, min_trust_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        q = SemanticQuery(metrics=["revenue"], context="board_reporting")
        with pytest.raises(GuardrailBlock) as e1:
            compile(q, min_trust_resolver, sources)
        with pytest.raises(GuardrailBlock) as e2:
            compile(q, min_trust_resolver, sources)
        assert str(e1.value) == str(e2.value)


class TestMinTrustAllowsMetFloor:
    """S5 AC1 (converse): the same answer outside the context returns normally."""

    def test_succeeds_when_tier_meets_level(
        self, min_trust_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], context="internal_dashboard"),
            min_trust_resolver,
            sources,
        )
        assert result.sql


class TestMinTrustCountsAssertionHistory:
    """A metric whose assertion passed in the harness meets a ``trusted`` floor.

    Regression: the floor was checked without the persisted verdicts, so every metric
    scored ``provisional`` there and a ``trusted`` floor refused even metrics the served
    trust score already reported as ``trusted``.
    """

    @pytest.fixture
    def asserted_resolver(
        self,
        revenue_binding: MetricBinding,
        refund_guardrail: Guardrail,
        min_trust_trusted_guardrail: Guardrail,
    ) -> ContractResolver:
        return ContractResolver(
            bindings=[revenue_binding],
            guardrails=[refund_guardrail, min_trust_trusted_guardrail],
            assertions=[
                Assertion(
                    id="revenue-q1",
                    query={"metrics": ["revenue"]},
                    expect=AssertionExpect(rows=1, values={"revenue": 100}),
                )
            ],
        )

    @staticmethod
    def _history(passed: bool) -> AssertionHistory:
        record = AssertionRecord(
            ts="2026-10-01T00:00:00Z", assertion_id="revenue-q1", passed=passed
        )
        return AssertionHistory({"orders.total_revenue": record})

    def test_passing_assertion_meets_the_floor(
        self, asserted_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], context="board_reporting"),
            asserted_resolver,
            sources,
            assertion_history=self._history(passed=True),
        )
        assert result.sql

    def test_failing_assertion_still_blocks(
        self, asserted_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock):
            compile(
                SemanticQuery(metrics=["revenue"], context="board_reporting"),
                asserted_resolver,
                sources,
                assertion_history=self._history(passed=False),
            )

    def test_without_history_the_assertion_is_unverified(
        self, asserted_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock):
            compile(
                SemanticQuery(metrics=["revenue"], context="board_reporting"),
                asserted_resolver,
                sources,
            )


class TestMinTrustNoop:
    def test_noop_when_context_absent(
        self, min_trust_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(SemanticQuery(metrics=["revenue"]), min_trust_resolver, sources)
        assert result.sql

    def test_noop_when_different_context(
        self, min_trust_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], context="some_other_context"),
            min_trust_resolver,
            sources,
        )
        assert result.sql


class TestTrustInputsCarried:
    """CompileResult.trust_inputs is populated for every compile path (SPEC-E14 §4)."""

    def test_trust_inputs_present_on_result(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(SemanticQuery(metrics=["revenue"]), resolver, sources)
        assert len(result.trust_inputs) == 1
        assert result.trust_inputs[0].metric == "revenue"
        assert result.trust_inputs[0].provenance == "human_curated"
        assert result.trust_inputs[0].has_assertion is False


class TestMinTrustSeverityWarn:
    """severity: warn does not block; it annotates warnings[] and guardrails_fired instead."""

    @pytest.fixture
    def warn_resolver(self, revenue_binding: MetricBinding) -> ContractResolver:
        guardrail = Guardrail(
            id="board-reporting-trusted-only",
            applies_to=AppliesTo(metric="revenue"),
            kind=GuardrailKind.MIN_TRUST,
            level="trusted",
            context="board_reporting",
            severity=Severity.WARN,
            rationale="Board figures should come from human-approved, final definitions.",
        )
        return ContractResolver(bindings=[revenue_binding], guardrails=[guardrail])

    def test_warns_instead_of_blocking(
        self, warn_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], context="board_reporting"), warn_resolver, sources
        )
        assert result.sql
        assert any("board-reporting-trusted-only" in w for w in result.warnings)

    def test_guardrails_fired_reports_warn_severity(
        self, warn_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], context="board_reporting"), warn_resolver, sources
        )
        fired = [g for g in result.guardrails_fired if g.id == "board-reporting-trusted-only"]
        assert fired and fired[0].severity == "warn"
