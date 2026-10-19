---
summary: "Gross margin rate is gross result over revenue. Its numerator contains revenue, so it is declared as revenue plus the cost of goods sold."
tags: [gross margin, margin, cost of goods sold, cogs, definition]
sl_refs:
  - coffeehouse_duckdb.order_items.cost
  - coffeehouse_duckdb.order_items
metrics: [gross_margin_rate, gross_result, cogs]
refs: [what-counts-as-a-sale]
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-10-01T00:00:00Z"
---

## Gross margin rate

**Gross margin rate = gross result / revenue**, where the gross result is revenue plus the
cost of goods sold. The cost is booked negative on every order line, so a cost lowers the
result. Both are aggregated over the requested period first, divided last.

| 2026 | Revenue | Cost of goods sold | Gross result | Gross margin rate |
| --- | --- | --- | --- | --- |
| January | 295.00 | -122.00 | 173.00 | **58.6%** |
| February | 370.00 | -176.00 | 194.00 | **52.4%** |
| March | 115.00 | -45.00 | 70.00 | **60.9%** |

The numerator contains the denominator, so the rate is `1 + cogs / revenue`. The metric
declares that as `numerator_parts: [revenue, cogs]`. When the rate changes, the useful
question is how much came from the cost of goods sold and how much from revenue, not from
"the numerator" and "the denominator", because higher revenue raises both.

From January to February the rate fell by 6.2 points. The cost of goods sold alone would
have lowered it by 16.4 points, and the higher revenue spread the cost over more sales and
gave back 10.2. By category, most of the cost increase is equipment: two hand grinders in
February against one in January, and a grinder keeps only 35% of its price.

Only sales count, see [[what-counts-as-a-sale]].
