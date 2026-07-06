---
summary: "What units_sold means and how it is calculated."
tags: [units_sold, definitions, metrics, order_items]
sl_refs:
  - warehouse_pg.order_items.units_sold
  - warehouse_pg.order_items
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-17T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.order_items.units_sold": "sha256:f9d568fe8feca201b8e8fde76b4f72941550591233aeba42d91614b90f6aeb6b"
---

**Units sold** is the total number of product units shipped across all order line items, excluding
refunded orders (the `revenue-excludes-refunds` guardrail propagates through joins).

The live expression: rendered directly from the semantic layer:

> `{{ sl:warehouse_pg.order_items.units_sold.expr }}`

## Grain

One row per **order line item**, not per order. The measure is fully additive across all
dimensions reachable from `order_items`: product category, sales channel (via the join
chain `order_items → orders → channels`), customer country (via `order_items → orders → customers`).

## What it counts

Each line item records the `quantity` of a single product in a single order. `units_sold` sums
that quantity. One order with two line items: 3 units of product A and 2 units of product B —
contributes 5 to `units_sold`.

## What it excludes

Line items belonging to orders with `status = 'refunded'` are excluded by the
`revenue-excludes-refunds` guardrail, which fires whenever a query reaches `orders.total_revenue`
through the join path. See [[revenue-excludes-refunds-caveat]] for the business rationale.

In the ecommerce demo data the total across non-refunded orders is **33 units**.
