---
summary: Joining orders to order_items fans out rows — never sum order amounts from the order_items table.
tags: [orders, order_items, fanout, caveat]
sl_refs: [order_items.units_sold, orders.revenue]
usage_mode: caveat
provenance: human_curated
---

The `order_items` table has one row per line item. A single order with 3 items appears as 3 rows in `order_items`.

If you join `orders` to `order_items` without aggregating at the order level first, the `amount` column on `orders` will be duplicated — once per line item. This inflates revenue.

**Safe pattern:** aggregate `order_items.quantity` for `units_sold`; use `orders.amount` for revenue (never through the join).
