---
# Notion page properties:
#   "Canon Type"   (select):       caveat
#   "Canon Topics" (multi-select): order_items, fanout, units_sold, line_revenue
canon_type: caveat
canon_topics: [order_items, fanout, units_sold, line_revenue]
---

# Order Items Fanout Trap

`order_items` lives at a **finer grain** than `orders` — multiple line items share the same
`order_id`. If you join the two fact tables and then aggregate an order-level measure
(`orders.amount`), each order's amount gets counted once per line item.

An order with 3 line items contributes its amount **3 times** to the total. The result looks
plausible but is wrong.

## What is safe

- Query `units_sold` or `line_revenue` from `order_items` alone (or via its declared joins to
  `products` and `orders`). The compiler enforces the `many_to_one` grain.
- Query `revenue` or `order_count` from `orders` alone (or via its declared joins to `customers`
  and `channels`).

## What is not supported (yet)

Querying `revenue` (from `orders`) and `units_sold` (from `order_items`) in the **same**
`query()` call crosses fact tables at different grains. This is blocked in Phase 1.

If you need both in one view: run two separate queries and join on `order_id` in your BI layer.

## Reconciliation check

`line_revenue` and `revenue` should agree when compared at the order grain (excluding refunded
orders). In the ecommerce demo data both equal **3790.50**. A discrepancy signals either a
data error or an accidental cross-fact join.

## How this becomes a Canon knowledge page

Canon ingests this page as `DocEvidence` with `usage_hint: caveat`. E6 auto-surfaces it
whenever `order_items.units_sold` or `order_items.line_revenue` appears in a search result.
