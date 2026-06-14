"""Fixtures for semantic-source tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# The SPEC-E5 §2.1 `orders` example, trimmed to a self-consistent, valid source.
VALID_SOURCE_YAML = """\
name: orders
connection: warehouse_pg
table: analytics.fct_orders
grain: [order_id]
description: "One row per order."
columns:
  - { name: order_id,    type: string,    nullable: false }
  - { name: customer_id, type: string,    nullable: false }
  - { name: status,      type: string,    nullable: false }
  - { name: amount,      type: decimal,   nullable: false }
  - { name: created_at,  type: timestamp, nullable: false }
measures:
  - name: total_revenue
    expr: "sum(amount)"
    additivity: additive
  - name: order_count
    expr: "count(distinct order_id)"
    additivity: non_additive
dimensions:
  - { name: status,     column: status }
  - { name: order_date, column: created_at, granularity: day }
joins:
  - to: customers
    on: "orders.customer_id = customers.customer_id"
    relationship: many_to_one
filters:
  - { name: completed, expr: "status = 'completed'" }
meta:
  provenance: inferred
"""


@pytest.fixture
def valid_source_yaml() -> str:
    """A valid semantic-source YAML string (SPEC-E5 §2.1 example)."""
    return VALID_SOURCE_YAML


@pytest.fixture
def write_source(tmp_path: Path):
    """Return a helper that writes YAML to a file under tmp_path and returns its Path."""

    def _write(content: str, name: str = "orders.yaml") -> Path:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    return _write


@pytest.fixture
def tmp_semantics_dir(tmp_path: Path) -> Path:
    """A project root with two semantic sources under nested connection dirs."""
    pg = tmp_path / "semantics" / "warehouse_pg"
    pg.mkdir(parents=True)
    (pg / "orders.yaml").write_text(VALID_SOURCE_YAML)
    (pg / "customers.yaml").write_text(
        "name: customers\n"
        "connection: warehouse_pg\n"
        "table: analytics.dim_customers\n"
        "grain: [customer_id]\n"
        "columns:\n"
        "  - { name: customer_id, type: string, nullable: false }\n"
        "  - { name: name,        type: string, nullable: true }\n"
    )
    return tmp_path
