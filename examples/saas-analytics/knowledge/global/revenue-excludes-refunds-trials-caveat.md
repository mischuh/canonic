---
summary: "gross_revenue silently excludes refunded and trial invoices — enforced by guardrails, not convention."
tags: [revenue, refunds, trials, guardrails, caveat]
sl_refs:
  - saas_duckdb.fct_invoices.total_amount
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-28T00:00:00Z"
---

## What is removed and why

Every query against `gross_revenue` (`fct_invoices.total_amount`) has two **mandatory_filter**
guardrails injected by the compiler, `severity: error`:

- `revenue-excludes-refunds` → `status != 'refunded'`. Refunds are accounting reversals, not revenue.
- `revenue-excludes-trials` → `is_trial = false`. Trial invoices are $0-value evaluations.

These filters are enforced by the compiler, not by analyst convention — they cannot be accidentally
omitted.

## Implication for analyses

When you reconcile `gross_revenue` against a system that reports *gross* billings (before refunds and
including trials), expect a gap equal to the refunded + trial amounts for the period. In this demo's
seed that gap is **98.00** (one 49.00 refund + one 49.00 trial invoice in 2025-Q1).

## Final vs provisional

For the latest open period, `fct_invoices_rt` holds provisional intraday estimates. The
`board-reporting-final-only` `restrict_source` guardrail confines `gross_revenue` to the final
`fct_invoices` source whenever the query runs in the `board_reporting` context. See
[[revenue-finality-policy]].
