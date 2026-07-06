---
summary: "What rental_revenue means, which source is authoritative, and what it excludes."
tags: [rental_revenue, revenue, definitions, metrics, payments]
sl_refs:
  - rental_db.payments.total_paid
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-22T00:00:00Z"
---

**Rental revenue** is the total amount collected from customers across all settled payment transactions.

The live expression: rendered directly from the semantic layer:

> `{{ sl:rental_db.payments.total_paid.expr }}`

## What it counts

Each `payments` row records one payment transaction. `total_paid` sums only rows where
`status = 'settled'`, excluding pending, failed, and refunded transactions. Because a single
rental may have multiple payments, `total_paid` is defined on the `payments` fact: not on
`rentals.total_amount`.

## What it excludes

| Excluded | Reason |
|---|---|
| Pending payments | Not yet collected: outstanding charge |
| Failed payments | Payment attempt did not clear |
| Refunded payments | Revenue already reversed |
| Active / confirmed rentals | No payment settled yet |
| Cancelled / no-show rentals | Typically no payment, or a refunded deposit |

## How it differs from related measures

| Measure | Source | Counts |
|---|---|---|
| `rental_revenue` | payments | settled payment amounts only |
| `rentals.total_base_revenue` | rentals | planned total for completed rentals: **do not use for financial reporting** |
| `damages.total_repair_cost` | damages | vehicle repair costs: separate from rental revenue |

## Grain

One row per `payment_id`. The measure is fully additive across payment date, payment method,
customer country, membership tier, vehicle category, and pickup location.

## Cross-fact fanout warning

See [[no-damage-payment-join]] before joining `damages` to `payments` in the same query.
Repair costs and rental revenue must be analysed in separate queries bridged through `rentals`.
