"""Tests for resolve_topic_refs — DocEvidence topic_ref candidates -> live sl_refs.

SPEC-E3 §5 fetch/extract-split amendment: topic_refs are candidates only; this is the one
place a candidate string becomes a fully-qualified sl_ref (or stays unresolved for review).
"""

from __future__ import annotations

from canonic.knowledge.resolve import resolve_topic_refs
from canonic.semantic.models import Column, Dimension, Measure, NormalizedType, SemanticSource


def _orders() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type=NormalizedType.STRING, nullable=False),
            Column(name="region", type=NormalizedType.STRING, nullable=False),
            Column(name="amount", type=NormalizedType.DECIMAL, nullable=False),
        ],
        measures=[Measure(name="mrr", expr="sum(amount)")],
        dimensions=[Dimension(name="region", column="region", aliases=["geo"])],
    )


def _customers() -> SemanticSource:
    return SemanticSource(
        name="customers",
        connection="warehouse_pg",
        table="analytics.dim_customers",
        grain=["customer_id"],
        columns=[Column(name="customer_id", type=NormalizedType.STRING, nullable=False)],
        measures=[Measure(name="mrr", expr="sum(1)")],
    )


def test_exact_measure_name_match_is_case_insensitive() -> None:
    resolved, unresolved = resolve_topic_refs(["MRR"], [_orders()])
    assert resolved == ["warehouse_pg.orders.mrr"]
    assert unresolved == []


def test_dimension_name_match() -> None:
    resolved, unresolved = resolve_topic_refs(["region"], [_orders()])
    assert resolved == ["warehouse_pg.orders.region"]
    assert unresolved == []


def test_dimension_alias_match() -> None:
    resolved, unresolved = resolve_topic_refs(["geo"], [_orders()])
    assert resolved == ["warehouse_pg.orders.region"]
    assert unresolved == []


def test_unmatched_candidate_is_unresolved() -> None:
    resolved, unresolved = resolve_topic_refs(["nonexistent_thing"], [_orders()])
    assert resolved == []
    assert unresolved == ["nonexistent_thing"]


def test_empty_topic_refs_returns_empty_lists() -> None:
    assert resolve_topic_refs([], [_orders()]) == ([], [])


def test_mixed_resolved_and_unresolved_preserve_order() -> None:
    resolved, unresolved = resolve_topic_refs(["MRR", "bogus", "geo"], [_orders()])
    assert resolved == ["warehouse_pg.orders.mrr", "warehouse_pg.orders.region"]
    assert unresolved == ["bogus"]


def test_ambiguous_name_across_sources_resolves_to_first_source_in_order() -> None:
    """Both sources declare a 'mrr' measure — first source in `sources` order wins."""
    resolved, _ = resolve_topic_refs(["mrr"], [_orders(), _customers()])
    assert resolved == ["warehouse_pg.orders.mrr"]


def test_no_measure_aliases_supported() -> None:
    """Measure has no aliases field at all — only its declared name is matchable."""
    resolved, unresolved = resolve_topic_refs(["monthly_recurring_revenue"], [_orders()])
    assert resolved == []
    assert unresolved == ["monthly_recurring_revenue"]
