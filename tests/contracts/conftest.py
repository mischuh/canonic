"""Fixtures for contract-surface tests (SPEC-E15 §2.2–2.5)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# SPEC-E15 §2.2 revenue example
VALID_BINDING_YAML = """\
metric: revenue
owner: "@data-platform"
canonical:
  source: orders
  measure: total_revenue
provenance: human_curated
aliases: ["net revenue", "rev"]
deprecated_alternatives:
  - { source: metabase, ref: "question:412", reason: "gross, includes refunds" }
status: active
"""

# SPEC-E15 §2.3 guardrail example
VALID_GUARDRAIL_YAML = """\
id: revenue-excludes-refunds
applies_to:
  source: orders
  measure: total_revenue
kind: mandatory_filter
filter: "status != 'refunded'"
severity: error
rationale: "Refunds are reversals, not revenue."
phase: P0
"""

# Guardrail referencing by metric name
METRIC_GUARDRAIL_YAML = """\
id: revenue-metric-guard
applies_to:
  metric: revenue
kind: mandatory_filter
filter: "status != 'refunded'"
severity: warn
rationale: "Guard via metric name."
"""

# SPEC-E15 §2.4 finality rule stub
VALID_FINALITY_YAML = """\
metric: revenue
realizations:
  - { source: orders, role: final, watermark: "business_day - 1 day", tz: "America/New_York" }
  - { source: orders_rt, role: provisional }
coalescing: "window <= watermark ? final : provisional"
result_flag: per_row
board_only_final: true
"""

# SPEC-E15 §2.5 assertion stub
VALID_ASSERTION_YAML = """\
id: revenue-2025-q1
query:
  metrics: [revenue]
  filters: ["order_date in 2025-Q1"]
expect:
  rows: 1
  values:
    revenue: 4218334.10
  tolerance: 0.01
source_of_truth: "Finance close, FY25 Q1"
"""

# Minimal semantic source so cross-surface validation has something to check against
ORDERS_SEMANTIC_YAML = """\
name: orders
connection: warehouse_pg
table: analytics.fct_orders
grain: [order_id]
columns:
  - { name: order_id, type: string, nullable: false }
  - { name: amount,   type: decimal, nullable: false }
  - { name: status,   type: string, nullable: false }
measures:
  - name: total_revenue
    expr: "sum(amount)"
    additivity: additive
"""


@pytest.fixture
def valid_binding_yaml() -> str:
    return VALID_BINDING_YAML


@pytest.fixture
def valid_guardrail_yaml() -> str:
    return VALID_GUARDRAIL_YAML


@pytest.fixture
def tmp_contracts_dir(tmp_path: Path) -> Path:
    """A project root with one binding, one guardrail, and a matching semantic source."""
    metrics_dir = tmp_path / "contracts" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "revenue.yaml").write_text(VALID_BINDING_YAML)

    guardrails_dir = tmp_path / "contracts" / "guardrails"
    guardrails_dir.mkdir(parents=True)
    (guardrails_dir / "revenue-excludes-refunds.yaml").write_text(VALID_GUARDRAIL_YAML)

    assertions_dir = tmp_path / "contracts" / "assertions"
    assertions_dir.mkdir(parents=True)
    (assertions_dir / "revenue-2025-q1.yaml").write_text(VALID_ASSERTION_YAML)

    semantics_dir = tmp_path / "semantics" / "warehouse_pg"
    semantics_dir.mkdir(parents=True)
    (semantics_dir / "orders.yaml").write_text(ORDERS_SEMANTIC_YAML)

    return tmp_path
