---
summary: Revenue is the total order amount (subtotal + tax). It is additive across orders and time.
tags: [revenue, orders, finance]
sl_refs: [orders.revenue]
usage_mode: definition
provenance: human_curated
---

Revenue is calculated as the `amount` column on the `orders` table, which equals `subtotal + tax_paid`.

All orders contribute to revenue regardless of payment method (bank transfer, credit card, coupon, or gift card). Revenue is fully additive: you can sum it across any time window or customer segment without double-counting.

Do not confuse `subtotal` (pre-tax) with `amount` (post-tax). When reporting revenue to finance, always use `amount`.
