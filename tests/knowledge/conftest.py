"""Fixtures for knowledge-page tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from canon.knowledge.models import KnowledgePage, KnowledgeScope
from canon.knowledge.validation import EntityIndex, PageIndex
from canon.semantic.models import Column, Dimension, Measure, NormalizedType, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable

# The SPEC-E6 §2 example page, trimmed to a self-consistent sample.
VALID_PAGE_MD = """\
---
summary: "Why test accounts are excluded from active-customer counts."
tags: [customers, definitions]
sl_refs:
  - warehouse_pg.customers
  - warehouse_pg.orders.total_revenue
refs: [test-account-policy]
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-14T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.orders.total_revenue": "sha256:abc"
  frozen: false
---

Body in Markdown. Inline [[test-account-policy]] resolves to another page.
A measure is referenced, never restated — {{ sl:warehouse_pg.orders.total_revenue.expr }}.
"""


@pytest.fixture
def valid_page_md() -> str:
    """A valid knowledge-page Markdown string (SPEC-E6 §2 example)."""
    return VALID_PAGE_MD


@pytest.fixture
def write_page(tmp_path: Path):
    """Write page content to ``knowledge/<rel_path>`` under tmp_path; return its Path."""

    def _write(content: str, rel_path: str = "global/customers-active.md") -> Path:
        p = tmp_path / "knowledge" / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    return _write


@pytest.fixture
def entity_index() -> EntityIndex:
    """Live entity index covering the `orders` and `customers` sources (SPEC-E5 §2.1)."""
    orders = SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type=NormalizedType.STRING, nullable=False),
            Column(name="customer_id", type=NormalizedType.STRING, nullable=False),
            Column(name="amount", type=NormalizedType.DECIMAL, nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)")],
        dimensions=[Dimension(name="order_status", column="customer_id")],
    )
    customers = SemanticSource(
        name="customers",
        connection="warehouse_pg",
        table="analytics.dim_customers",
        grain=["customer_id"],
        columns=[Column(name="customer_id", type=NormalizedType.STRING, nullable=False)],
    )
    return EntityIndex.from_sources([orders, customers])


@pytest.fixture
def make_page() -> Callable[..., KnowledgePage]:
    """Factory for KnowledgePage models with sensible defaults; override per test."""

    def _make(
        slug: str = "customers-active",
        *,
        scope: KnowledgeScope = KnowledgeScope.GLOBAL,
        sl_refs: list[str] | None = None,
        refs: list[str] | None = None,
        body: str = "",
    ) -> KnowledgePage:
        return KnowledgePage(
            id=slug,
            path=Path("knowledge") / scope.value / f"{slug}.md",
            scope=scope,
            sl_refs=sl_refs or [],
            refs=refs or [],
            body=body,
        )

    return _make


@pytest.fixture
def page_index() -> PageIndex:
    """Page index with one GLOBAL page and one USER page."""
    return PageIndex(
        slugs_by_scope={
            KnowledgeScope.GLOBAL: frozenset({"test-account-policy"}),
            KnowledgeScope.USER: frozenset({"my-private-note"}),
        }
    )
