---
summary: "Board reporting uses final, settled revenue only; provisional intraday figures are for operational dashboards."
tags: [revenue, finality, governance, policy]
sl_refs:
  - saas_duckdb.fct_invoices.total_amount
  - saas_duckdb.fct_invoices_rt.total_amount
usage_mode: policy
meta:
  provenance: human_curated
  last_validated_at: "2026-06-28T00:00:00Z"
---

## Policy

Revenue exists in two physical realizations:

- **`fct_invoices`** — the authoritative, settled (final) billing record. Watermark: `business_day - 1 day`.
- **`fct_invoices_rt`** — provisional intraday estimates for the current open period.

The `finality-revenue` rule coalesces them: rows within the watermark are served from the final
source, fresher rows from the provisional source, and each row is flagged accordingly.

For **board reporting** the `board-reporting-final-only` guardrail (`restrict_source`, `severity: error`)
forces `gross_revenue` onto the final source only — board figures never include unsettled estimates.
Run such queries with `"context": "board_reporting"`.

Operational dashboards that want the freshest possible number omit the context and accept the
provisional/final blend. See [[revenue-excludes-refunds-trials-caveat]].
