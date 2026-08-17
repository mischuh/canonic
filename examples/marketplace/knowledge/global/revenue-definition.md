---
summary: "What revenue means and how it is calculated."
tags: [public, revenue, definitions, metrics, orders]
sl_refs:
  - marketplace_db.orders.revenue
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-08-10T00:00:00Z"
---

**Revenue** is the total amount charged to customers across all non-cancelled orders.

The live expression: rendered directly from the semantic layer:

> `{{ sl:marketplace_db.orders.revenue.expr }}`

## What it counts

Each row in `orders` represents one order placed with a merchant. `revenue` sums
`amount`, which is the post-discount total: if a promotion applied, `amount` already
reflects the promotion's `discount_pct` off the order subtotal.

## What it excludes

The `orders-excludes-cancelled` guardrail requires `status != 'cancelled'` whenever
`revenue` is aggregated: cancelled orders were never actually paid, so summing them in
would overstate settled sales. `pending` and `refunded` orders are still included by
default — filter explicitly on `status` if you need settled-only figures.

## Tenant scoping

`orders` is a tenant-scoped source (`contracts/policies/tenancy.yaml`): every query
against `revenue` is automatically filtered to the caller's resolved `merchant_id`.
There is no way to see another merchant's revenue through this metric.

## How it differs from related measures

| Measure | Source | Counts |
|---|---|---|
| `revenue` | orders | post-discount order totals, cancelled excluded |
| `order_count` | orders | number of orders, any status |
| `aov` | ratio | `revenue / order_count` |
| `items_sold` | order_items | unit quantity across line items (platform-only metric) |

## Typical values

In the marketplace demo data, monthly revenue per merchant ranges from a few
thousand to tens of thousands of dollars, depending on merchant size and the month's
promotion activity.
