---
# Notion page properties:
#   "Canon Type"   (select):       caveat
#   "Canon Topics" (multi-select): revenue, refunds, guardrails
canon_type: caveat
canon_topics: [revenue, refunds, guardrails]
---

# Revenue Excludes Refunds

Every revenue figure in this company excludes refunded orders. This is not a convention — it is
enforced by the `revenue-excludes-refunds` guardrail, which automatically injects
`status != 'refunded'` into every query that touches `total_revenue`. You cannot accidentally
include refunds by forgetting a WHERE clause.

## Why

A refund is an accounting reversal. Including it would:

1. Overstate gross revenue and period-over-period growth.
2. Produce a number that disagrees with the finance team's reconciled P&L.

## What this means when comparing to external systems

If you compare Canon revenue figures to a third-party payment processor or an ERP that
records **gross** amounts (before refunds), expect a gap. The gap equals the sum of refunded
order amounts in the comparison period.

In the ecommerce demo data this gap is **260.00** (orders 2 and 8, both `status = 'refunded'`).

## How this becomes a Canon knowledge page

Canon ingests this page as `DocEvidence` with `usage_hint: caveat`. E6 writes it as a
`caveat`-mode knowledge page and **auto-surfaces** it alongside any search result that touches
`orders.total_revenue` — so the warning rides along even if the original query was about
reporting policy, not refunds.
