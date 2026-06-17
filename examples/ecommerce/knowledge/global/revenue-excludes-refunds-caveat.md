---
summary: "Refunded orders are silently excluded from every revenue query — here's why."
tags: [revenue, caveats, guardrails]
sl_refs:
  - warehouse_pg.orders.total_revenue
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-17T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.orders.total_revenue": "sha256:6193c9db63810d49d8c08c18bacb053c943f8f0c1ea8d0fc3dee16ecc0cd7b34"
---

## Why refunds are excluded

A refund is an accounting reversal, not revenue. Including refunded amounts would overstate
both gross revenue and period-over-period growth, and would cause the revenue figure to
disagree with the finance team's reconciled P&L.

The exclusion is **enforced by the `revenue-excludes-refunds` guardrail** (`severity: error`),
not by convention — the compiler injects `status != 'refunded'` into every query that touches
`warehouse_pg.orders.total_revenue`. It cannot be accidentally omitted.

## Implication for analyses

When you compare revenue to a third-party system that records gross amounts (before refunds),
expect a gap. The gap equals the sum of refunded order amounts in the comparison period.

In the ecommerce demo data this is **260.00** (orders 2 and 8, both status `refunded`).
