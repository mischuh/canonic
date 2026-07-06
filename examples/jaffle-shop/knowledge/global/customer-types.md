---
summary: Customers are classified as 'personal' or 'business'. Business accounts typically place higher-value orders.
tags: [customers, segmentation, customer_type]
sl_refs: [orders.customer_type]
usage_mode: definition
provenance: human_curated
---

The `customer_type` field on the `customers` table has two values:

- **`personal`**: individual consumer accounts
- **`business`**: B2B accounts that often order in bulk or have negotiated pricing

When slicing revenue or order count by customer segment, use `customer_type` from the `orders` semantic model (which carries it as a categorical dimension from the join to `customers`).
