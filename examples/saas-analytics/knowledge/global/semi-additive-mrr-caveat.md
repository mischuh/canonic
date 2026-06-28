---
summary: "Never sum MRR across months — it is a snapshot measure. Use ending_mrr (collapse: last)."
tags: [mrr, snapshot, semi-additive, caveat]
sl_refs:
  - saas_duckdb.fct_mrr_snapshot.mrr_sum
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-28T00:00:00Z"
---

## Why you cannot sum MRR over time

`fct_mrr_snapshot` records one row **per customer per month** — it is a *snapshot* fact. The same
recurring revenue is restated every month it stays on the books. Summing `mrr` across twelve monthly
snapshots therefore counts the same subscription up to twelve times and overstates the figure by
roughly an order of magnitude.

## The safe pattern

The `ending_mrr` metric is bound as **semi-additive** (`collapse_dimension: snapshot_month`,
`collapse_agg: last`). It is additive across customers/segments/plans *within* a month, but when a
result spans multiple months it collapses to the **last** month's position instead of summing.

- Roll MRR up over time → use `ending_mrr` (let the binding collapse it).
- Need a within-month additive sum (e.g. as a ratio numerator) → use `mrr_total`, and always group
  by `snapshot_month`.

The `ending-mrr-requires-month` guardrail records the expectation that `ending_mrr` be grouped by
`snapshot_month`. See [[mrr-definition]] for the full definition.
