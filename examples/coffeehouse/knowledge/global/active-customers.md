---
summary: "An active customer has at least one completed order in the period. Count distinct customers per period, never sum."
tags: [active customers, customers, count distinct, definition]
sl_refs:
  - coffeehouse_duckdb.orders
refs: [what-counts-as-a-sale]
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-10-01T00:00:00Z"
---

## Active customers

A customer is **active** in a period when they placed at least one completed order in it.
The number is a distinct count over that whole period: Q1 2026 has **5** active customers
(Anna, Ben, Clara, Dario, Emma). The QA test customer never counts.

See [[distinct-counts-do-not-add-up]] before rolling the number up.
