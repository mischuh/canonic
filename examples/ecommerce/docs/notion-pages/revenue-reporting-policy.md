---
# Notion page properties:
#   "Canon Type"   (select):       policy
#   "Canon Topics" (multi-select): revenue, policy, reporting
canon_type: policy
canon_topics: [revenue, policy, reporting]
---

# Revenue Reporting Policy

## Reporting period cutoff

Revenue is attributed to the **order creation date** (`order_date`), not the payment settlement
date. The cutoff is midnight UTC at the end of the last calendar day of the reporting period.

- Orders that arrive **before** midnight are in-scope for that period.
- Orders that arrive **at or after** midnight roll into the next period.

This aligns with the Finance team's general ledger accrual date.

## Pending orders

Pending orders are **included** in reported revenue by default. Finance reviews and adjusts the
pending bucket during month-end close before publishing the official figure.

If you need the finance-approved number (pending orders excluded), apply an additional filter
`status = 'completed'` and note the exclusion in your report.

## Cross-system reconciliation

The canonical source for revenue reconciliation is the `orders` table in the warehouse. Any
discrepancy with a third-party payment processor or the ERP should be raised with the
**Finance Data team** before the figure is published.

Do not reconcile against a system that records gross amounts — see the *Revenue Excludes Refunds*
page for why net and gross figures differ.

## How this becomes a Canon knowledge page

Canon ingests this page as `DocEvidence` with `usage_hint: policy`. E6 writes it as a
`policy`-mode knowledge page, searchable via `search_knowledge("revenue reporting")`.
