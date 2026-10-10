---
summary: "Never average an order total over order lines: multi-line baskets are counted once per line and inflate AOV."
tags: [aov, average, order lines, fanout, caveat]
sl_refs:
  - coffeehouse_duckdb.order_items
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-10-01T00:00:00Z"
---

## Why AVG(order_total) on a line table is wrong

`order_items` has one row per line. A wide table that repeats the order total on every line
(a common dbt mart shape) turns `AVG(order_total)` into an average over **lines**: Ben's
four-line 200.00 basket in January is counted four times, Anna's 20.00 single-line order once.

| January 2026 | Value |
| --- | --- |
| AVG(order_total) over 9 lines | 102.22 |
| revenue / distinct orders (295.00 / 5) | **59.00** |

The definition reads correctly and runs without error. It is only right when every order has
exactly one line. Use the `average_order_value` metric, a ratio of `revenue` and
`order_count` (a distinct count of `order_id`).
