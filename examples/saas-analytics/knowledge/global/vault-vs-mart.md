---
summary: "When to query the business-vault facts vs the pre-aggregated data marts."
tags: [warehouse, data-vault, data-mart, modelling, reference]
sl_refs:
  - saas_duckdb.fct_mrr_snapshot
  - saas_duckdb.mart_monthly_mrr
usage_mode: reference
meta:
  provenance: human_curated
  last_validated_at: "2026-06-28T00:00:00Z"
---

## Two layers, two jobs

This warehouse is modelled Kimball-style in two layers:

**Business Vault** — the `dim_*` and `fct_*` tables. Atomic grain, fully dimensional, joinable.
Query these (via their metrics) when you need flexibility: arbitrary slicing, ad-hoc dimensions,
exact recomputation. Almost every metric in this project binds to a vault fact.

**Data Marts** — the `mart_*` tables. Pre-aggregated, condensed, fixed grain:

- `mart_monthly_mrr` — MRR / movement by month × segment.
- `mart_cohort_retention` — signup-cohort retention curves.
- `mart_account_health` — one health + LTV row per customer (source of the opaque `customer_ltv`).
- `mart_rep_quota` — rep quota attainment by quarter.

Query a mart when you want the condensed answer cheaply and at exactly its grain. Marts trade
flexibility for speed and stability, and they are the natural home for pre-computed values that
should not be re-aggregated — see [[ltv-methodology]].

## Rule of thumb

Need to slice freely or recompute from atoms → vault facts. Need a fast, fixed, condensed roll-up →
data mart. When a metric is bound to a mart (e.g. `customer_ltv`), respect the mart's grain.
