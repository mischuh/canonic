---
summary: "Joining order_items back to orders inflates order-level metrics: the classic fanout trap."
tags: [order_items, caveats, fanout, line_revenue, units_sold]
sl_refs:
  - warehouse_pg.order_items.units_sold
  - warehouse_pg.order_items.line_revenue
  - warehouse_pg.order_items
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-17T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.order_items.units_sold": "sha256:f9d568fe8feca201b8e8fde76b4f72941550591233aeba42d91614b90f6aeb6b"
    "warehouse_pg.order_items.line_revenue": "sha256:9fc157b77e889e06ce807110bb25f92de9e5e452ea32cd31bff7dc3daad3c980"
---

## The fanout problem

`fct_order_items` is at a **finer grain** than `fct_orders`: multiple line items share the same
`order_id`. If you join the two fact tables and then aggregate `orders.amount`, each order's amount
gets duplicated once per line item. An order with 3 line items contributes its amount 3 times.

**Safe:** query `units_sold` or `line_revenue` from `order_items` alone (or via its declared joins
to `products` and `orders`). The compiler enforces the declared `many_to_one` grain and keeps
aggregation correct.

**Unsafe:** query `revenue` (from `orders`) and `units_sold` (from `order_items`) together in a
single `query()` call: this crosses fact tables at different grains and is not supported in P0.
Use two separate queries and join on `order_id` in your BI layer if you need both in one view.

## Reconciliation check

`line_revenue` and `revenue` should agree when compared at the order grain (excluding refunded
orders). In the ecommerce demo both equal **3790.50**. A discrepancy signals either a seed-data
error or an inadvertent cross-fact join.
