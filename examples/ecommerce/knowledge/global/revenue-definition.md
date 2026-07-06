---
summary: "What total_revenue means and how it is calculated."
tags: [revenue, definitions, metrics]
sl_refs:
  - warehouse_pg.orders.total_revenue
  - warehouse_pg.orders
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-17T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.orders.total_revenue": "sha256:6193c9db63810d49d8c08c18bacb053c943f8f0c1ea8d0fc3dee16ecc0cd7b34"
---

**Total revenue** is the sum of completed and pending order amounts, excluding refunded orders.

The live expression: rendered directly from the semantic layer, so this definition can never drift:

> `{{ sl:warehouse_pg.orders.total_revenue.expr }}`

## What it includes

- All orders with `status = 'completed'` or `status = 'pending'`.
- Multi-currency orders are recorded in the transaction currency; no FX conversion is applied in v1.

## What it excludes

Refunded orders are removed by the `revenue-excludes-refunds` guardrail on every query: the filter
is enforced by the compiler, not by convention. See the [[revenue-excludes-refunds-caveat]] page for
the business rationale.

## Grain

One row per `order_id`. The measure is fully additive across all dimensions declared on `orders`.
