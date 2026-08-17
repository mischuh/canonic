---
summary: "What average order value (aov) means and how the ratio is computed."
tags: [merchant, aov, definitions, metrics, orders]
sl_refs:
  - marketplace_db.orders.revenue
  - marketplace_db.orders.order_count
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-08-10T00:00:00Z"
---

**Average order value (AOV)** is the mean amount charged per order: `revenue` divided
by `order_count`.

## Grain

`aov` is a `ratio`-kind canonical metric (`contracts/metrics/aov.yaml`): it is not a
single physical measure, it is `revenue / order_count` computed at query time, on the
grain of whatever dimensions the query groups by.

## Zero-denominator handling

If a reporting slice has zero orders, `aov` returns `null` rather than dividing by
zero or silently returning 0 — a period with no orders has no meaningful average
order value.

## Tenant scoping

Both `revenue` and `order_count` are defined on the tenant-scoped `orders` source, so
`aov` is automatically scoped to the caller's resolved `merchant_id` the same way.

## How it differs from related measures

| Measure | Meaning |
|---|---|
| `aov` | mean amount per order (revenue / order_count) |
| `revenue` | total amount charged, summed across orders |
| `order_count` | number of orders, unweighted by amount |

## Typical usage

AOV is most useful compared across a promotion window: a merchant running a
percentage-off promotion typically sees `order_count` rise and `aov` dip slightly
during the window, since the discount reduces the per-order amount even as more
orders come in.
