---
summary: "A sale is a completed order by a real customer. Refunds, cancellations and QA test orders never count."
tags: [revenue, sales, refunds, test-customers, policy]
sl_refs:
  - coffeehouse_duckdb.order_items.revenue
  - coffeehouse_duckdb.order_items.units_sold
  - coffeehouse_duckdb.orders
usage_mode: policy
meta:
  provenance: human_curated
  last_validated_at: "2026-10-01T00:00:00Z"
---

## What counts as a sale

Only orders with `status = 'completed'` placed by a real customer count. That rules out:

- **Refunded orders.** The money went back to the customer.
- **Cancelled orders.** The money was never taken.
- **QA test orders.** The customer `QA Test` (`is_test = true`) exists to test checkout. Its
  orders look real, including one 600.00 grinder order in January.

Revenue is `{{ sl:coffeehouse_duckdb.order_items.revenue.expr }}` over the remaining order
lines.

This is not left to the person asking. The guardrails `sales-completed-orders-only` and
`sales-exclude-test-customers` are compiled into every query on `order_items`, and their
`orders-*` twins into every query on `orders`. A metric added tomorrow inherits them without
anyone remembering to.
