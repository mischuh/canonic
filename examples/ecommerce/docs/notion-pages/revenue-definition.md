---
# Notion page properties — set these in the Notion sidebar before connecting Canon.
# The Notion connector reads them deterministically; no LLM is involved.
#   "Canon Type"   (select):       definition
#   "Canon Topics" (multi-select): revenue, metrics, definitions
canon_type: definition
canon_topics: [revenue, metrics, definitions]
---

# Revenue Definition

**Total revenue** is the sum of order amounts for completed and pending orders, net of refunds.

## What it includes

- Orders with `status = 'completed'` or `status = 'pending'`.
- Each order records a single `amount` in the transaction currency. No FX conversion is applied
  in v1 — multi-currency orders are summed at face value.

## What it excludes

Refunded orders. A refund is an accounting reversal, not revenue. Including refunded amounts
overstates period-over-period growth and produces a figure that disagrees with the finance
team's reconciled P&L.

The exclusion is automatically enforced: the `revenue-excludes-refunds` guardrail injects
`status != 'refunded'` into every query that touches this metric. It cannot be accidentally omitted.

## Grain

One row per order. The metric is fully additive across all dimensions available on the `orders`
source — customer country, sales channel, and order date.

## How this becomes a Canon knowledge page

When Canon ingests this Notion page, it produces a `DocEvidence` record with:
- `title`: "Revenue Definition"
- `body`: the prose above
- `usage_hint`: `definition` (from the *Canon Type* property)
- `topic_refs`: `["revenue", "metrics", "definitions"]` (from *Canon Topics* — resolved as candidates)
- `acquisition_tier`: `hand_authored`

E6 then writes this as a `definition`-mode knowledge page and surfaces it in `search_knowledge("revenue definition")`.
