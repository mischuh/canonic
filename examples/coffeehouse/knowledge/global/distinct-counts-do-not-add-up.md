---
summary: "Distinct counts are not additive: never sum daily or monthly active customers into a longer period."
tags: [count distinct, active customers, daily, monthly, rollup, caveat]
sl_refs:
  - coffeehouse_duckdb.orders
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-10-01T00:00:00Z"
---

## Why summing active customers overcounts

Anna buys almost every week. A daily table of distinct customers counts her once per day she
buys, so summing the days counts her many times.

| Q1 2026 | Value |
| --- | --- |
| sum of daily active customers | 15 |
| sum of monthly active customers (3 + 4 + 3) | 10 |
| distinct customers over the quarter | **5** |

Each daily figure is correct on its own. The roll-up is not. The `active_customers` metric
is recomputed from orders at the requested grain, so a quarter is never derived from its
parts. The same holds for `order_count` and for every ratio built on them, such as
`revenue_per_customer`.
