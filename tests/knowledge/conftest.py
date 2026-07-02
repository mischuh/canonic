"""Fixtures for knowledge-page tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from canonic.knowledge.models import (
    KnowledgePage,
    KnowledgePageMeta,
    KnowledgeScope,
    UsageMode,
)
from canonic.knowledge.validation import EntityIndex, PageIndex
from canonic.semantic.models import Column, Dimension, Measure, NormalizedType, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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
        user: str | None = None,
        sl_refs: list[str] | None = None,
        refs: list[str] | None = None,
        body: str = "",
        meta: KnowledgePageMeta | None = None,
    ) -> KnowledgePage:
        # USER pages need a knowledge/user/<id>/… path; default owner is "alice".
        if scope is KnowledgeScope.USER:
            path = Path("knowledge") / scope.value / (user or "alice") / f"{slug}.md"
        else:
            path = Path("knowledge") / scope.value / f"{slug}.md"
        return KnowledgePage(
            id=slug,
            path=path,
            scope=scope,
            sl_refs=sl_refs or [],
            refs=refs or [],
            body=body,
            meta=meta or KnowledgePageMeta(),
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


@pytest.fixture
def make_search_page() -> Callable[..., KnowledgePage]:
    """Factory for retrieval-test pages: lets a test set summary/tags/usage_mode/body."""

    def _make(
        page_id: str,
        *,
        scope: KnowledgeScope = KnowledgeScope.GLOBAL,
        user: str = "alice",
        summary: str = "",
        tags: list[str] | None = None,
        body: str = "",
        usage_mode: UsageMode = UsageMode.REFERENCE,
        sl_refs: list[str] | None = None,
    ) -> KnowledgePage:
        if scope is KnowledgeScope.USER:
            path = Path("knowledge") / "user" / user / f"{page_id}.md"
        else:
            path = Path("knowledge") / "global" / f"{page_id}.md"
        return KnowledgePage(
            id=page_id,
            path=path,
            scope=scope,
            summary=summary,
            tags=tags or [],
            body=body,
            usage_mode=usage_mode,
            sl_refs=sl_refs or [],
        )

    return _make


class KeywordEmbedder:
    """Deterministic stub embedder for the vector arm — no model download (SPEC-E6 §5.1).

    Each synonym group shares one embedding dimension, so texts that mention any word in a
    group are similar even when they share no literal token (e.g. ``revenue`` ≈ ``sales``).
    This lets a test create a vector-only hit (similar but not lexically matching) and a
    both-arms hit, deterministically.
    """

    def __init__(self, groups: list[list[str]], *, identity: str = "keyword-stub@v1") -> None:
        self._word_to_dim: dict[str, int] = {}
        for dim, group in enumerate(groups):
            for word in group:
                self._word_to_dim[word] = dim
        self._dim = len(groups)
        self._identity = identity

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"[a-z]+", text.lower()):
                dim = self._word_to_dim.get(word)
                if dim is not None:
                    out[i, dim] += 1.0
        return out

    def model_identity(self) -> str:
        return self._identity


@pytest.fixture
def keyword_embedder() -> KeywordEmbedder:
    """Stub embedder whose synonym groups cover the words used in retrieval tests."""
    return KeywordEmbedder(
        [
            ["sales", "revenue", "earnings"],
            ["customer", "customers", "client"],
            ["weather", "forecast"],
        ]
    )
