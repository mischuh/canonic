---
summary: "What MRR (ending_mrr) means and how it is calculated from the snapshot fact."
tags: [mrr, revenue, definitions, metrics]
sl_refs:
  - saas_duckdb.fct_mrr_snapshot.mrr_sum
  - saas_duckdb.fct_mrr_snapshot
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-28T00:00:00Z"
---

**Monthly Recurring Revenue (MRR)** is the recurring subscription revenue on the books at the
end of a month. Canonic exposes it as the `ending_mrr` metric.

The live expression: rendered directly from the semantic layer, so this definition can never drift:

> `{{ sl:saas_duckdb.fct_mrr_snapshot.mrr_sum.expr }}`

## How it is computed

`ending_mrr` is a **semi-additive** metric bound to the `fct_mrr_snapshot` snapshot fact with
`collapse_dimension: snapshot_month` and `collapse_agg: last`. Within a single month it sums the
active MRR across all customers; across multiple months it takes the **last** month's position
rather than summing: see [[semi-additive-mrr-caveat]].

## Grain

One row per customer per month in `fct_mrr_snapshot`. MRR is additive across customers, segments,
plans and geographies *within* a month. Churned customers carry `mrr = 0` and `is_active = false`.

## Related metrics

- `mrr_total`: the plain additive sum (used as the numerator of `arpu`); query it grouped by month.
- `active_subscribers`: distinct active customers in a month.
- `arpu`: `mrr_total / active_accounts`.
