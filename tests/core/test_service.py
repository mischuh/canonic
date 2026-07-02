"""Tests for the CanonicService capability layer (canonic/core/service.py)."""

from __future__ import annotations

import pytest

from canonic.compiler.query import SemanticQuery
from canonic.config import CanonicConfig
from canonic.contracts.models import CanonicalRef, Example, ExampleQuery, MetricBinding, Status
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import DimensionInfo, MetricDetail, MetricSummary, OverviewResult
from canonic.core.service import CanonicService
from canonic.exc import Ambiguous, Unresolved


class TestListMetrics:
    def test_returns_active_bindings(self, canonic_service: CanonicService) -> None:
        summaries = canonic_service.list_metrics()
        assert len(summaries) == 1
        s = summaries[0]
        assert isinstance(s, MetricSummary)
        assert s.metric == "revenue"
        assert s.kind == "single"
        assert s.source == "orders"
        assert s.measure == "total_revenue"
        assert s.status == "active"
        assert "rev" in s.aliases
        assert s.components is None

    def test_is_sorted_and_deduplicated(self, canonic_service: CanonicService) -> None:
        summaries = canonic_service.list_metrics()
        metrics = [s.metric for s in summaries]
        assert metrics == sorted(metrics)
        assert len(metrics) == len(set(metrics))

    def test_includes_dimensions(self, canonic_service: CanonicService) -> None:
        summaries = canonic_service.list_metrics()
        s = next(s for s in summaries if s.metric == "revenue")
        assert len(s.dimensions) > 0
        assert all(isinstance(d, DimensionInfo) for d in s.dimensions)
        assert any(d.name == "order_date" for d in s.dimensions)


class TestListMetricsDistinctCount:
    def test_distinct_count_appears_in_list(self, distinct_count_service: CanonicService) -> None:
        summaries = distinct_count_service.list_metrics()
        assert len(summaries) == 1
        s = summaries[0]
        assert s.metric == "unique_customers"
        assert s.kind == "distinct_count"
        assert s.source == "orders"
        assert s.measure == "order_id"
        assert s.status == "active"
        assert s.components is None

    def test_percentile_appears_in_list(self, percentile_service: CanonicService) -> None:
        summaries = percentile_service.list_metrics()
        assert len(summaries) == 1
        s = summaries[0]
        assert s.metric == "median_rental_amount"
        assert s.kind == "percentile"
        assert s.source == "orders"
        assert s.measure == "amount"
        assert s.status == "active"
        assert s.components is None


class TestListMetricsComposite:
    def test_ratio_appears_in_list(self, ratio_service: CanonicService) -> None:
        summaries = ratio_service.list_metrics()
        names = {s.metric for s in summaries}
        assert "avg_cost" in names
        ratio = next(s for s in summaries if s.metric == "avg_cost")
        assert ratio.kind == "ratio"
        assert ratio.source is None
        assert ratio.measure is None
        assert ratio.components == ["revenue", "damage_count"]

    def test_ratio_components_also_listed(self, ratio_service: CanonicService) -> None:
        summaries = ratio_service.list_metrics()
        names = {s.metric for s in summaries}
        assert {"avg_cost", "revenue", "damage_count"} <= names

    def test_weighted_avg_appears_in_list(self, weighted_avg_service: CanonicService) -> None:
        summaries = weighted_avg_service.list_metrics()
        names = {s.metric for s in summaries}
        assert "avg_weighted_cost" in names
        wa = next(s for s in summaries if s.metric == "avg_weighted_cost")
        assert wa.kind == "weighted_avg"
        assert wa.source is None
        assert wa.measure is None
        assert wa.components == ["revenue", "damage_count"]


class TestDescribeMetric:
    def test_happy_path(self, canonic_service: CanonicService) -> None:
        detail = canonic_service.describe_metric("revenue")
        assert isinstance(detail, MetricDetail)
        assert detail.metric == "revenue"
        assert detail.source == "orders"
        assert detail.measure == "total_revenue"
        assert "order_id" in detail.grain
        assert any(d.name == "order_date" for d in detail.dimensions)
        assert "total_revenue" in detail.measures
        assert "rev" in detail.aliases

    def test_alias_lookup(self, canonic_service: CanonicService) -> None:
        detail = canonic_service.describe_metric("rev")
        assert detail.metric == "revenue"

    def test_unknown_raises_unresolved(self, canonic_service: CanonicService) -> None:
        with pytest.raises(Unresolved):
            canonic_service.describe_metric("mrr")


class TestDescribeMetricDistinctCount:
    def test_returns_detail(self, distinct_count_service: CanonicService) -> None:
        detail = distinct_count_service.describe_metric("unique_customers")
        assert isinstance(detail, MetricDetail)
        assert detail.metric == "unique_customers"
        assert detail.source == "orders"
        assert detail.measure is None
        assert any(d.name == "order_date" for d in detail.dimensions)
        assert "active_customers" in detail.aliases

    def test_alias_lookup(self, distinct_count_service: CanonicService) -> None:
        detail = distinct_count_service.describe_metric("active_customers")
        assert detail.metric == "unique_customers"


class TestDescribeMetricExamples:
    def test_examples_empty_by_default(self, canonic_service: CanonicService) -> None:
        detail = canonic_service.describe_metric("revenue")
        assert detail.examples == []

    def test_examples_populated_from_binding(
        self,
        orders_source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ex = Example(
            query=ExampleQuery(metrics=["revenue"], dimensions=["order_date"]),
            origin="observed_query",
            frequency=10,
        )
        binding = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            status=Status.ACTIVE,
            examples=[ex],
        )
        monkeypatch.setenv("PG_PASSWORD", "pw")
        from tests.core.conftest import _DC_CONFIG

        svc = CanonicService(
            config=CanonicConfig.model_validate(_DC_CONFIG),
            resolver=ContractResolver(bindings=[binding], guardrails=[]),
            sources=[orders_source],
        )
        detail = svc.describe_metric("revenue")
        assert len(detail.examples) == 1
        assert detail.examples[0].query.dimensions == ["order_date"]
        assert detail.examples[0].frequency == 10


class TestGetOverview:
    def test_returns_overview_result(self, canonic_service: CanonicService) -> None:
        result = canonic_service.get_overview()
        assert isinstance(result, OverviewResult)

    def test_groups_by_source(self, canonic_service: CanonicService) -> None:
        result = canonic_service.get_overview()
        assert any(g.name == "orders" for g in result.domains)

    def test_metrics_listed_in_group(self, canonic_service: CanonicService) -> None:
        result = canonic_service.get_overview()
        orders_group = next(g for g in result.domains if g.name == "orders")
        assert any(m.name == "revenue" for m in orders_group.metrics)

    def test_dimensions_on_group(self, canonic_service: CanonicService) -> None:
        result = canonic_service.get_overview()
        orders_group = next(g for g in result.domains if g.name == "orders")
        assert "order_date" in orders_group.dimensions

    def test_sample_questions_not_empty(self, canonic_service: CanonicService) -> None:
        result = canonic_service.get_overview()
        for group in result.domains:
            assert group.sample_questions, f"domain {group.name!r} has empty sample_questions"

    def test_domain_filter(self, canonic_service: CanonicService) -> None:
        result = canonic_service.get_overview(domain="orders")
        assert len(result.domains) == 1
        assert result.domains[0].name == "orders"

    def test_unknown_domain_returns_empty(self, canonic_service: CanonicService) -> None:
        result = canonic_service.get_overview(domain="nonexistent")
        assert result.domains == []

    def test_sample_questions_from_examples(
        self,
        orders_source,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ex = Example(
            query=ExampleQuery(metrics=["revenue"], dimensions=["region"]),
            origin="observed_query",
            frequency=38,
        )
        binding = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            status=Status.ACTIVE,
            examples=[ex],
        )
        monkeypatch.setenv("PG_PASSWORD", "pw")
        from tests.core.conftest import _DC_CONFIG

        svc = CanonicService(
            config=CanonicConfig.model_validate(_DC_CONFIG),
            resolver=ContractResolver(bindings=[binding], guardrails=[]),
            sources=[orders_source],
        )
        result = svc.get_overview()
        orders_group = next(g for g in result.domains if g.name == "orders")
        assert any("region" in q for q in orders_group.sample_questions)


class TestResolveMetric:
    def test_happy_path(self, canonic_service: CanonicService) -> None:
        from canonic.contracts.resolver import Binding

        binding = canonic_service.resolve_metric("revenue")
        assert isinstance(binding, Binding)
        assert binding.metric == "revenue"
        assert binding.source == "orders"

    def test_unknown_raises_unresolved(self, canonic_service: CanonicService) -> None:
        with pytest.raises(Unresolved, match="no active binding"):
            canonic_service.resolve_metric("unknown_metric")

    def test_ambiguous_raises_ambiguous(
        self,
        orders_source,
        refund_guardrail,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from canonic.config import CanonicConfig
        from canonic.contracts.models import CanonicalRef, MetricBinding, Status
        from canonic.contracts.resolver import ContractResolver

        monkeypatch.setenv("PG_PASSWORD", "pw")
        b1 = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            status=Status.ACTIVE,
        )
        b2 = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            status=Status.ACTIVE,
        )
        resolver = ContractResolver(bindings=[b1, b2], guardrails=[])
        config = CanonicConfig.model_validate(
            {
                "version": 1,
                "project": {"name": "test"},
                "connections": [],
                "llm": {
                    "provider": "openai_compatible",
                    "base_url": "http://localhost/v1",
                    "model": "llama3",
                },
            }
        )
        svc = CanonicService(config=config, resolver=resolver, sources=[orders_source])
        with pytest.raises(Ambiguous, match="ambiguous"):
            svc.resolve_metric("revenue")


class TestDescribeMetricComposite:
    def test_ratio_returns_combined_dimensions(self, ratio_service: CanonicService) -> None:
        detail = ratio_service.describe_metric("avg_cost")
        assert isinstance(detail, MetricDetail)
        assert detail.source is None
        assert detail.measure is None
        assert detail.grain == []
        dim_names = {d.name for d in detail.dimensions}
        assert "order_date" in dim_names
        assert "status" in dim_names

    def test_ratio_aliases_preserved(self, ratio_service: CanonicService) -> None:
        detail = ratio_service.describe_metric("avg_cost")
        assert "cost ratio" in detail.aliases

    def test_weighted_avg_returns_dimensions(self, weighted_avg_service: CanonicService) -> None:
        detail = weighted_avg_service.describe_metric("avg_weighted_cost")
        assert detail.source is None
        assert len(detail.dimensions) > 0


class TestCompileQuery:
    def test_compiles_to_sql(self, canonic_service: CanonicService) -> None:
        q = SemanticQuery(metrics=["revenue"])
        result = canonic_service.compile_query(q)
        assert "SELECT" in result.sql.upper()
        assert result.resolved == {"revenue": "orders.total_revenue"}
        assert any(g.id == "revenue-excludes-refunds" for g in result.guardrails_fired)

    def test_unresolved_metric_raises(self, canonic_service: CanonicService) -> None:
        from canonic import exc

        q = SemanticQuery(metrics=["unknown"])
        with pytest.raises(exc.Unresolved):
            canonic_service.compile_query(q)
