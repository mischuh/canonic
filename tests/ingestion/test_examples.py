"""Unit tests for canon/ingestion/examples.py — collect_examples and ExampleEnricher.

Acceptance criteria from GH-156:
AC1 (S13.1): binding with ≥1 observed query includes examples sourced only from evidence / assertions.
AC2 (S13.2): metric with no evidence has examples: [] — not absent, not fabricated.
AC3:  examples re-computed each run; expired evidence drops off.
AC4:  origin is a typed, non-null string; origin_kind branches without string heuristics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from canon.connectors.base import (
    AcquisitionTier,
    UsageDefinition,
    UsageEvidence,
    UsageRole,
)
from canon.contracts.models import (
    Assertion,
    AssertionExpect,
    CanonicalRef,
    Example,
    ExampleOriginKind,
    MetricBinding,
)
from canon.ingestion.examples import ExampleEnricher, collect_examples
from canon.ingestion.models import (
    EvidenceItem,
    EvidenceKind,
    ReconciliationDecision,
    ReconciliationReport,
)

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assertion(
    id: str = "revenue-2025-q1",
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    filters: list[str] | None = None,
) -> Assertion:
    query: dict[str, Any] = {"metrics": metrics or ["revenue"]}
    if dimensions:
        query["dimensions"] = dimensions
    if filters:
        query["filters"] = filters
    return Assertion(id=id, query=query, expect=AssertionExpect())


def _observed_item(
    relations: list[str],
    frequency: int = 10,
    fingerprint: str = "sha256:obs1",
) -> EvidenceItem:
    return EvidenceItem(
        source="warehouse",
        kind=EvidenceKind.OBSERVED_QUERY,
        acquisition_tier=AcquisitionTier.QUERY_HISTORY,
        payload={
            "sql_normalized": "select sum(amount) from orders",
            "relations": relations,
            "frequency": frequency,
        },
        source_fingerprint=fingerprint,
        observed_at=_NOW,
    )


def _usage_item(
    references: list[str],
    artifact: str = "question:412",
    frequency: int = 5,
    fingerprint: str = "sha256:usage1",
) -> EvidenceItem:
    ev = UsageEvidence(
        source="metabase_prod",
        artifact=artifact,
        title="Revenue BI question",
        defines=UsageDefinition(expr="sum(amount)", references=references),
        role=UsageRole.TRUSTED_EXAMPLE,
        frequency=frequency,
        native_ref=f"metabase:{artifact}",
        source_fingerprint=fingerprint,
        observed_at=_NOW,
    )
    return EvidenceItem(
        source="metabase_prod",
        kind=EvidenceKind.USAGE_EVIDENCE,
        acquisition_tier=AcquisitionTier.QUERY_HISTORY,
        payload=ev.model_dump(mode="json"),
        source_fingerprint=fingerprint,
        observed_at=_NOW,
    )


def _metrics_for_relation(mapping: dict[str, list[str]]) -> Any:
    return lambda rel: mapping.get(rel, [])


# ---------------------------------------------------------------------------
# collect_examples unit tests
# ---------------------------------------------------------------------------


class TestCollectExamplesNoEvidence:
    def test_returns_empty_list_when_no_evidence(self) -> None:
        """AC2: metric with no evidence → examples: [], never fabricated."""
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({}),
            assertions=[],
            evidence=[],
        )
        assert result == []

    def test_returns_empty_list_when_evidence_does_not_touch_metric(self) -> None:
        item = _observed_item(relations=["analytics.fct_orders"])
        result = collect_examples(
            "orders_count",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[item],
        )
        assert result == []


class TestCollectExamplesObservedQuery:
    def test_observed_query_touching_relation_produces_example(self) -> None:
        """AC1: observed query resolves to metric via relation."""
        item = _observed_item(relations=["analytics.fct_orders"], frequency=38)
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[item],
        )
        assert len(result) == 1
        assert result[0].origin == "observed_query"
        assert result[0].origin_kind is ExampleOriginKind.OBSERVED_QUERY
        assert result[0].frequency == 38
        assert result[0].query.metrics == ["revenue"]

    def test_observed_query_carries_nonnull_origin(self) -> None:
        """AC4: origin is non-null typed string."""
        item = _observed_item(relations=["fct_orders"])
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[item],
        )
        assert result[0].origin is not None
        assert result[0].origin_kind is ExampleOriginKind.OBSERVED_QUERY

    def test_observed_query_uses_short_relation_name(self) -> None:
        """Fully-qualified relation 'analytics.fct_orders' resolves via short name."""
        item = _observed_item(relations=["analytics.fct_orders"])
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[item],
        )
        assert len(result) == 1

    def test_observed_queries_ranked_by_frequency(self) -> None:
        low = _observed_item(relations=["fct_orders"], frequency=2, fingerprint="sha256:a")
        high = _observed_item(relations=["fct_orders"], frequency=99, fingerprint="sha256:b")
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[low, high],
        )
        assert result[0].frequency == 99
        assert result[1].frequency == 2


class TestCollectExamplesAssertions:
    def test_assertion_referencing_metric_produces_example(self) -> None:
        """AC1: executable assertion whose metrics includes the metric."""
        a = _assertion(metrics=["revenue"], filters=["order_date in 2025-Q1"])
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({}),
            assertions=[a],
            evidence=[],
        )
        assert len(result) == 1
        assert result[0].origin == "assertion:revenue-2025-q1"
        assert result[0].origin_kind is ExampleOriginKind.ASSERTION
        assert result[0].frequency is None
        assert result[0].query.filters == ["order_date in 2025-Q1"]

    def test_assertion_referencing_alias_produces_example(self) -> None:
        a = _assertion(metrics=["rev"])
        result = collect_examples(
            "revenue",
            aliases=["rev", "net revenue"],
            metrics_for_relation=_metrics_for_relation({}),
            assertions=[a],
            evidence=[],
        )
        assert len(result) == 1

    def test_non_executable_assertion_excluded(self) -> None:
        """Candidate assertions (no metrics) must be excluded."""
        a = Assertion(
            id="candidate",
            query={"native": "sum(amount)", "references": ["fct_orders"]},
            expect=AssertionExpect(),
        )
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({}),
            assertions=[a],
            evidence=[],
        )
        assert result == []

    def test_assertion_for_different_metric_excluded(self) -> None:
        a = _assertion(metrics=["orders_count"])
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({}),
            assertions=[a],
            evidence=[],
        )
        assert result == []


class TestCollectExamplesUsageEvidence:
    def test_usage_evidence_touching_relation_produces_example(self) -> None:
        item = _usage_item(references=["analytics.fct_orders"], frequency=15)
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[item],
        )
        assert len(result) == 1
        assert result[0].origin_kind is ExampleOriginKind.USAGE_EVIDENCE
        assert result[0].origin == "usage_evidence:question:412"
        assert result[0].frequency == 15

    def test_usage_evidence_origin_is_nonnull(self) -> None:
        """AC4."""
        item = _usage_item(references=["fct_orders"])
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[item],
        )
        assert result[0].origin is not None


class TestCollectExamplesRankingAndTrim:
    def test_at_most_three_examples_returned(self) -> None:
        evidence = [
            _observed_item(relations=["fct_orders"], frequency=i, fingerprint=f"sha256:{i}")
            for i in range(5)
        ]
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[_assertion()],
            evidence=evidence,
        )
        assert len(result) <= 3

    def test_observed_ranked_before_assertions(self) -> None:
        obs = _observed_item(relations=["fct_orders"], frequency=1)
        a = _assertion()
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[a],
            evidence=[obs],
        )
        assert result[0].origin_kind is ExampleOriginKind.OBSERVED_QUERY
        assert result[1].origin_kind is ExampleOriginKind.ASSERTION

    def test_assertions_ranked_before_usage_evidence(self) -> None:
        usage = _usage_item(references=["fct_orders"], frequency=99)
        a = _assertion()
        result = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[a],
            evidence=[usage],
        )
        assert result[0].origin_kind is ExampleOriginKind.ASSERTION
        assert result[1].origin_kind is ExampleOriginKind.USAGE_EVIDENCE


class TestCollectExamplesAC3:
    def test_expired_evidence_drops_off_on_rerun(self) -> None:
        """AC3: evidence not in current run is not present in examples."""
        obs = _observed_item(relations=["fct_orders"], frequency=10)
        first = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[obs],
        )
        assert len(first) == 1

        # Re-run with no evidence (evidence expired)
        second = collect_examples(
            "revenue",
            aliases=[],
            metrics_for_relation=_metrics_for_relation({"fct_orders": ["revenue"]}),
            assertions=[],
            evidence=[],
        )
        assert second == []


# ---------------------------------------------------------------------------
# ExampleEnricher integration tests
# ---------------------------------------------------------------------------


def _write_binding(tmp_path: Path, binding: MetricBinding) -> None:
    from canon.contracts.loader import dump_metric_binding

    slug = binding.metric.replace(" ", "_").lower()
    metrics_dir = tmp_path / "contracts" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / f"{slug}.yaml").write_text(dump_metric_binding(binding))


def _write_assertion(tmp_path: Path, assertion: Assertion) -> None:
    import yaml

    assertions_dir = tmp_path / "contracts" / "assertions"
    assertions_dir.mkdir(parents=True, exist_ok=True)
    (assertions_dir / f"{assertion.id}.yaml").write_text(
        yaml.dump(assertion.model_dump(mode="json"), default_flow_style=False)
    )


class TestExampleEnricher:
    def test_empty_evidence_empty_examples(self, tmp_path: Path) -> None:
        """AC2: enricher writes examples: [] when no evidence exists."""
        binding = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
        )
        _write_binding(tmp_path, binding)

        enricher = ExampleEnricher(tmp_path, [])
        report = enricher.enrich(ReconciliationReport(entries=[]))
        assert report.entries == []

    def test_assertion_example_attached_via_synthesised_edit(self, tmp_path: Path) -> None:
        """Assertion with no other binding report entry → synthesised EDIT entry."""
        binding = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
        )
        _write_binding(tmp_path, binding)
        a = _assertion(metrics=["revenue"], filters=["order_date >= '2025-01-01'"])
        _write_assertion(tmp_path, a)

        enricher = ExampleEnricher(tmp_path, [])
        report = enricher.enrich(ReconciliationReport(entries=[]))

        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.EDIT
        assert entry.target == "contracts/metrics/revenue.yaml"
        examples = entry.proposal.content["examples"]
        assert len(examples) == 1
        assert examples[0]["origin"] == "assertion:revenue-2025-q1"
        assert examples[0]["frequency"] is None

    def test_observed_query_example_attached(self, tmp_path: Path) -> None:
        """AC1: observed query resolving to binding metric → example in output."""
        binding = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
        )
        _write_binding(tmp_path, binding)
        obs = _observed_item(relations=["orders"], frequency=38)

        enricher = ExampleEnricher(tmp_path, [obs])
        report = enricher.enrich(ReconciliationReport(entries=[]))

        assert len(report.entries) == 1
        examples = report.entries[0].proposal.content["examples"]
        assert examples[0]["origin"] == "observed_query"
        assert examples[0]["frequency"] == 38

    def test_no_change_when_examples_already_match(self, tmp_path: Path) -> None:
        """No EDIT synthesised when persisted examples already match computed ones."""
        example = Example(
            query={"metrics": ["revenue"]},  # type: ignore[arg-type]
            origin="observed_query",
            frequency=38,
        )
        # Write binding that already has the example baked in
        binding = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            examples=[example],
        )
        _write_binding(tmp_path, binding)
        obs = _observed_item(relations=["orders"], frequency=38)

        enricher = ExampleEnricher(tmp_path, [obs])
        report = enricher.enrich(ReconciliationReport(entries=[]))
        assert report.entries == []
