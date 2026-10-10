---
summary: "Average order value is revenue divided by the number of distinct orders, computed per period."
tags: [aov, average order value, basket size, definition]
sl_refs:
  - coffeehouse_duckdb.order_items.revenue
  - coffeehouse_duckdb.order_items
refs: [what-counts-as-a-sale]
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-10-01T00:00:00Z"
---

## Average order value (AOV)

**AOV = revenue / number of distinct orders**, both aggregated over the requested period
first, divided last.

In January 2026 that is 295.00 over 5 orders, so **59.00**.

Only sales count, see [[what-counts-as-a-sale]].
