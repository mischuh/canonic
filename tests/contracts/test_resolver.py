"""Tests for canonic/contracts/resolver.py — the contract↔compiler seam (SPEC-E5-E15 §6).

Covers the issue acceptance criteria: unknown→Unresolved, two active bindings→Ambiguous,
mandatory_filter returned for a matching source/measure, and determinism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.contracts.models import (
    AppliesTo,
    Assertion,
    AssertionExpect,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    RestrictTo,
    Status,
)
from canonic.contracts.resolver import (
    Ambiguous,
    Binding,
    ContractResolver,
    Unresolved,
)

if TYPE_CHECKING:
    from pathlib import Path


def _binding(
    metric: str,
    *,
    aliases: list[str] | None = None,
    status: Status = Status.ACTIVE,
    source: str = "orders",
    measure: str = "total_revenue",
) -> MetricBinding:
    return MetricBinding(
        metric=metric,
        canonical=CanonicalRef(source=source, measure=measure),
        aliases=aliases or [],
        status=status,
    )


def _guardrail(
    gid: str, *, source: str | None = None, measure: str | None = None, metric: str | None = None
) -> Guardrail:
    return Guardrail(
        id=gid,
        applies_to=AppliesTo(source=source, measure=measure, metric=metric),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="status != 'refunded'",
        rationale="test",
    )


class TestResolveMetric:
    def test_unknown_name_unresolved(self) -> None:
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[])
        result = resolver.resolve_metric("ghost")
        assert result == Unresolved(name="ghost")

    def test_single_match_binding(self) -> None:
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[])
        result = resolver.resolve_metric("revenue")
        assert isinstance(result, Binding)
        assert result.metric == "revenue"
        assert result.source == "orders"
        assert result.measure == "total_revenue"

    def test_alias_resolves_to_binding(self) -> None:
        resolver = ContractResolver(
            bindings=[_binding("revenue", aliases=["rev", "net revenue"])], guardrails=[]
        )
        result = resolver.resolve_metric("rev")
        assert isinstance(result, Binding)
        assert result.metric == "revenue"

    def test_two_active_bindings_ambiguous(self) -> None:
        # Two active bindings sharing the alias "rev"; loader would reject this across
        # files, so the ambiguity path is exercised via the direct constructor.
        a = _binding("revenue", aliases=["rev"])
        b = _binding("gross_revenue", aliases=["rev"], measure="gross_revenue")
        resolver = ContractResolver(bindings=[a, b], guardrails=[])
        result = resolver.resolve_metric("rev")
        assert isinstance(result, Ambiguous)
        assert result.name == "rev"
        assert {c.metric for c in result.candidates} == {"revenue", "gross_revenue"}

    def test_deprecated_binding_ignored(self) -> None:
        resolver = ContractResolver(
            bindings=[_binding("revenue", status=Status.DEPRECATED)], guardrails=[]
        )
        assert resolver.resolve_metric("revenue") == Unresolved(name="revenue")

    def test_determinism_identical_results(self) -> None:
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[])
        assert resolver.resolve_metric("revenue") == resolver.resolve_metric("revenue")
        assert resolver.resolve_metric("ghost") == resolver.resolve_metric("ghost")


class TestGuardrailsFor:
    def test_mandatory_filter_for_matching_source_measure(self) -> None:
        g = _guardrail("revenue-excludes-refunds", source="orders", measure="total_revenue")
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[g])
        result = resolver.guardrails_for("orders", "total_revenue")
        assert [x.id for x in result] == ["revenue-excludes-refunds"]

    def test_source_wide_guardrail_matches_any_measure(self) -> None:
        g = _guardrail("orders-guard", source="orders")  # no measure → source-wide
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[g])
        assert resolver.guardrails_for("orders", "total_revenue") == [g]

    def test_metric_targeted_guardrail_via_reverse_map(self) -> None:
        g = _guardrail("revenue-metric-guard", metric="revenue")
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[g])
        assert resolver.guardrails_for("orders", "total_revenue") == [g]

    def test_non_matching_guardrail_excluded(self) -> None:
        g = _guardrail("other-guard", source="customers", measure="signups")
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[g])
        assert resolver.guardrails_for("orders", "total_revenue") == []

    def test_metric_guard_for_deprecated_binding_excluded(self) -> None:
        g = _guardrail("dep-guard", metric="revenue")
        resolver = ContractResolver(
            bindings=[_binding("revenue", status=Status.DEPRECATED)], guardrails=[g]
        )
        assert resolver.guardrails_for("orders", "total_revenue") == []

    def test_stable_sort_by_id(self) -> None:
        gs = [
            _guardrail("zzz", source="orders"),
            _guardrail("aaa", source="orders"),
            _guardrail("mmm", source="orders"),
        ]
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=gs)
        ids = [g.id for g in resolver.guardrails_for("orders", "total_revenue")]
        assert ids == ["aaa", "mmm", "zzz"]

    def test_determinism_identical_order(self) -> None:
        gs = [_guardrail("b", source="orders"), _guardrail("a", source="orders")]
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=gs)
        first = resolver.guardrails_for("orders", "total_revenue")
        second = resolver.guardrails_for("orders", "total_revenue")
        assert [g.id for g in first] == [g.id for g in second]


def _restrict_guardrail(gid: str, *, metric: str, context: str) -> Guardrail:
    return Guardrail(
        id=gid,
        applies_to=AppliesTo(metric=metric),
        kind=GuardrailKind.RESTRICT_SOURCE,
        restrict_to=RestrictTo(role="final"),
        context=context,
        rationale="test restrict_source",
    )


class TestRestrictSourceFor:
    def test_returns_guardrail_on_matching_context(self) -> None:
        revenue = _binding("revenue")
        g = _restrict_guardrail("board-final-only", metric="revenue", context="board_reporting")
        resolver = ContractResolver(bindings=[revenue], guardrails=[g])
        result = resolver.restrict_source_for("orders", "total_revenue", "board_reporting")
        assert [r.id for r in result] == ["board-final-only"]

    def test_returns_empty_on_wrong_context(self) -> None:
        revenue = _binding("revenue")
        g = _restrict_guardrail("board-final-only", metric="revenue", context="board_reporting")
        resolver = ContractResolver(bindings=[revenue], guardrails=[g])
        result = resolver.restrict_source_for("orders", "total_revenue", "internal_dashboard")
        assert result == []

    def test_returns_empty_when_context_is_none(self) -> None:
        revenue = _binding("revenue")
        g = _restrict_guardrail("board-final-only", metric="revenue", context="board_reporting")
        resolver = ContractResolver(bindings=[revenue], guardrails=[g])
        result = resolver.restrict_source_for("orders", "total_revenue", None)
        assert result == []

    def test_does_not_return_mandatory_filter_guardrails(self) -> None:
        revenue = _binding("revenue")
        mf = _guardrail("mf", source="orders", measure="total_revenue")
        resolver = ContractResolver(bindings=[revenue], guardrails=[mf])
        result = resolver.restrict_source_for("orders", "total_revenue", "board_reporting")
        assert result == []

    def test_stable_sort_by_id(self) -> None:
        revenue = _binding("revenue")
        g1 = _restrict_guardrail("z-guard", metric="revenue", context="board_reporting")
        g2 = _restrict_guardrail("a-guard", metric="revenue", context="board_reporting")
        resolver = ContractResolver(bindings=[revenue], guardrails=[g1, g2])
        result = resolver.restrict_source_for("orders", "total_revenue", "board_reporting")
        assert [r.id for r in result] == ["a-guard", "z-guard"]


class TestP0Stubs:
    def test_finality_for_returns_none(self) -> None:
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[])
        assert resolver.finality_for("revenue") is None

    def test_assertions_for_empty_when_none_loaded(self) -> None:
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[])
        assert resolver.assertions_for({"metrics": ["revenue"]}) == []


class TestAssertionsFor:
    @staticmethod
    def _assertion(aid: str, metrics: list[str]) -> Assertion:
        return Assertion(id=aid, query={"metrics": metrics}, expect=AssertionExpect())

    def test_matches_same_metric_set(self) -> None:
        a = self._assertion("rev-q1", ["revenue"])
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[], assertions=[a])
        assert [x.id for x in resolver.assertions_for({"metrics": ["revenue"]})] == ["rev-q1"]

    def test_no_match_for_different_metrics(self) -> None:
        a = self._assertion("rev-q1", ["revenue"])
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[], assertions=[a])
        assert resolver.assertions_for({"metrics": ["profit"]}) == []

    def test_empty_query_metrics_matches_nothing(self) -> None:
        a = self._assertion("rev-q1", ["revenue"])
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[], assertions=[a])
        assert resolver.assertions_for({"metrics": []}) == []

    def test_non_executable_candidate_excluded(self) -> None:
        candidate = Assertion(id="usage-x", query={"native": "sum(amount)"})
        resolver = ContractResolver(
            bindings=[_binding("revenue")], guardrails=[], assertions=[candidate]
        )
        assert resolver.assertions_for({"metrics": ["revenue"]}) == []

    def test_all_assertions_returns_everything(self) -> None:
        a = self._assertion("rev-q1", ["revenue"])
        candidate = Assertion(id="usage-x", query={"native": "sum(amount)"})
        resolver = ContractResolver(
            bindings=[_binding("revenue")], guardrails=[], assertions=[a, candidate]
        )
        assert [x.id for x in resolver.all_assertions()] == ["rev-q1", "usage-x"]


class TestFromProject:
    def test_loads_and_resolves(self, tmp_contracts_dir: Path) -> None:
        resolver = ContractResolver.from_project(tmp_contracts_dir)
        result = resolver.resolve_metric("revenue")
        assert isinstance(result, Binding)
        assert result.source == "orders"
        assert result.measure == "total_revenue"

    def test_loads_guardrails(self, tmp_contracts_dir: Path) -> None:
        resolver = ContractResolver.from_project(tmp_contracts_dir)
        result = resolver.guardrails_for("orders", "total_revenue")
        assert [g.id for g in result] == ["revenue-excludes-refunds"]

    def test_alias_from_project(self, tmp_contracts_dir: Path) -> None:
        resolver = ContractResolver.from_project(tmp_contracts_dir)
        assert isinstance(resolver.resolve_metric("rev"), Binding)


class TestMetricsForSource:
    def test_returns_sorted_metrics_for_source(self) -> None:
        b1 = _binding("revenue", source="orders", measure="total_revenue")
        b2 = _binding("order_count", source="orders", measure="cnt")
        resolver = ContractResolver(bindings=[b1, b2], guardrails=[])
        assert resolver.metrics_for_source("orders") == ["order_count", "revenue"]

    def test_returns_empty_for_unknown_source(self) -> None:
        resolver = ContractResolver(bindings=[_binding("revenue")], guardrails=[])
        assert resolver.metrics_for_source("nonexistent") == []

    def test_excludes_inactive_bindings(self) -> None:
        active = _binding("revenue", source="orders", measure="total_revenue")
        inactive = MetricBinding(
            metric="deprecated_rev",
            canonical=CanonicalRef(source="orders", measure="old_revenue"),
            status=Status.DEPRECATED,
        )
        resolver = ContractResolver(bindings=[active, inactive], guardrails=[])
        assert resolver.metrics_for_source("orders") == ["revenue"]
