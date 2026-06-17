"""Fixtures for knowledge-page tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

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
