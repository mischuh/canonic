---
summary: "Month-end cutoff and reporting standards for the revenue metric."
tags: [revenue, policy, reporting]
sl_refs:
  - warehouse_pg.orders.total_revenue
  - warehouse_pg.orders
usage_mode: policy
meta:
  provenance: human_curated
  last_validated_at: "2026-06-17T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.orders.total_revenue": "sha256:6193c9db63810d49d8c08c18bacb053c943f8f0c1ea8d0fc3dee16ecc0cd7b34"
---

## Reporting period cutoff

Revenue is attributed to the **order creation date** (`order_date`), not the payment settlement
date. Cutoff is midnight UTC at the end of the last day of the reporting period.

Orders that arrive before midnight are in-scope; orders that arrive at or after midnight roll
into the next period. This aligns with the finance team's general ledger accrual date.

## Pending orders

Pending orders are **included** in reported revenue by default. Finance reviews and adjusts
the pending bucket during month-end close before publishing the official figure.

If you need the finance-approved number (pending excluded), apply `status = 'completed'` as
an additional filter and note the difference in your report.

## Cross-system reconciliation

The canonical source for reconciliation is the `orders` table in `warehouse_pg`. Any
discrepancies with third-party payment processors or the ERP should be raised with the
Finance Data team before publication.
