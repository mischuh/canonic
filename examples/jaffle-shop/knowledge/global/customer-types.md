---
summary: Customers are classified as 'personal' or 'business'. Business accounts typically place higher-value orders.
tags: [customers, segmentation, customer_type]
sl_refs: [jaffle_duckdb.customers.customer_type]
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-10-16T00:00:00Z"
---

The `customer_type` field on the `customers` table has two values:

- **`personal`**: individual consumer accounts
- **`business`**: B2B accounts that often order in bulk or have negotiated pricing

When slicing revenue or order count by customer segment, use the `customer_type` dimension of the `customers` semantic source. Orders reach it through the declared `many_to_one` join on `customer_id`.
