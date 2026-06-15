"""Tests for the CanonService capability layer (canon/core/service.py)."""

from __future__ import annotations

import pytest

from canon.compiler.query import SemanticQuery
from canon.core.models import MetricDetail, MetricSummary
from canon.core.service import CanonService
from canon.exc import Ambiguous, Unresolved


class TestListMetrics:
    def test_returns_active_bindings(self, canon_service: CanonService) -> None:
        summaries = canon_service.list_metrics()
        assert len(summaries) == 1
        s = summaries[0]
        assert isinstance(s, MetricSummary)
        assert s.metric == "revenue"
        assert s.source == "orders"
        assert s.measure == "total_revenue"
        assert s.status == "active"
        assert "rev" in s.aliases

    def test_is_sorted_and_deduplicated(self, canon_service: CanonService) -> None:
        summaries = canon_service.list_metrics()
        metrics = [s.metric for s in summaries]
        assert metrics == sorted(metrics)
        assert len(metrics) == len(set(metrics))


class TestDescribeMetric:
    def test_happy_path(self, canon_service: CanonService) -> None:
        detail = canon_service.describe_metric("revenue")
        assert isinstance(detail, MetricDetail)
        assert detail.metric == "revenue"
        assert detail.source == "orders"
        assert detail.measure == "total_revenue"
        assert "order_id" in detail.grain
        assert "order_date" in detail.dimensions
        assert "total_revenue" in detail.measures
        assert "rev" in detail.aliases

    def test_alias_lookup(self, canon_service: CanonService) -> None:
        detail = canon_service.describe_metric("rev")
        assert detail.metric == "revenue"

    def test_unknown_raises_unresolved(self, canon_service: CanonService) -> None:
        with pytest.raises(Unresolved):
            canon_service.describe_metric("mrr")


class TestResolveMetric:
    def test_happy_path(self, canon_service: CanonService) -> None:
        from canon.contracts.resolver import Binding

        binding = canon_service.resolve_metric("revenue")
        assert isinstance(binding, Binding)
        assert binding.metric == "revenue"
        assert binding.source == "orders"

    def test_unknown_raises_unresolved(self, canon_service: CanonService) -> None:
        with pytest.raises(Unresolved, match="no active binding"):
            canon_service.resolve_metric("unknown_metric")

    def test_ambiguous_raises_ambiguous(
        self,
        orders_source,
        refund_guardrail,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from canon.config import CanonConfig
        from canon.contracts.models import CanonicalRef, MetricBinding, Status
        from canon.contracts.resolver import ContractResolver

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
        config = CanonConfig.model_validate(
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
        svc = CanonService(config=config, resolver=resolver, sources=[orders_source])
        with pytest.raises(Ambiguous, match="ambiguous"):
            svc.resolve_metric("revenue")


class TestCompileQuery:
    def test_compiles_to_sql(self, canon_service: CanonService) -> None:
        q = SemanticQuery(metrics=["revenue"])
        result = canon_service.compile_query(q)
        assert "SELECT" in result.sql.upper()
        assert result.resolved == {"revenue": "orders.total_revenue"}
        assert any(g.id == "revenue-excludes-refunds" for g in result.guardrails_fired)

    def test_unresolved_metric_raises(self, canon_service: CanonService) -> None:
        from canon import exc

        q = SemanticQuery(metrics=["unknown"])
        with pytest.raises(exc.Unresolved):
            canon_service.compile_query(q)
