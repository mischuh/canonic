---
summary: "Platform take-rate and cross-merchant benchmarking notes — platform-ops only."
tags: [platform, margin, take-rate, internal]
sl_refs:
  - marketplace_db.order_items.items_sold
usage_mode: reference
meta:
  provenance: human_curated
  last_validated_at: "2026-08-10T00:00:00Z"
---

**Internal note, platform-ops audience only.** This page is deliberately tagged
`platform`, which is outside every merchant role's `knowledge.allow_tags`
(`merchant_viewer`/`merchant_admin` allow `public`/`merchant` only). Only
`platform_analyst` (`allow_tags: ["*"]`) can read it — it demonstrates that the
knowledge-visibility gate (SPEC-E12 §6) is enforced by tag, independent of tenant
scoping.

## Take-rate benchmarking

Cross-merchant comparisons of `items_sold` and `revenue` are only meaningful from the
`platform_analyst` role, since `tenancy_exempt: true` is what allows a single query to
span every merchant at once. A merchant-scoped role can never produce this comparison
even in principle: every query it runs is predicated to its own `merchant_id`.

## Why items_sold is platform-only

`items_sold` (defined on `order_items`) is intentionally absent from
`merchant_viewer`/`merchant_admin`'s `metrics.allow` list. It is visible only through
`platform_analyst`'s wildcard `allow: ["*"]`. This is not a technical limitation of the
metric itself — it is a deliberate authorization choice: unit-volume figures are
considered platform-competitive-sensitive information merchants should not be able
to query about each other, and the underlying `order_items` table only carries a
single merchant's rows once tenant scoping is applied anyway.

## Margin caveats

Gross merchandise value (GMV) figures derived from summing `revenue` across merchants
are pre-take-rate; do not present them as platform net revenue without applying the
per-category take-rate schedule (maintained outside this project).
