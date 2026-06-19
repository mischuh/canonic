---
summary: "How revenue is attributed to reporting periods in the Chinook music store."
tags: [revenue, policy, reporting]
sl_refs:
  - chinook_pg.invoices.total_revenue
  - chinook_pg.invoices
usage_mode: policy
meta:
  provenance: human_curated
  last_validated_at: "2026-06-18T00:00:00Z"
---

## Reporting period attribution

Revenue is attributed to the **invoice creation date** (`InvoiceDate`), which is the timestamp
when the purchase transaction was recorded. There is no separate payment date or settlement date
in the Chinook schema.

Cutoff is midnight UTC at the end of the last day of the reporting period. Invoices that arrive
before midnight are in-scope; invoices at or after midnight roll into the next period.

## No refund or cancellation states

The standard Chinook schema has no status column on `Invoice`. Every row represents a completed
sale. If your deployment adds cancellation or refund tracking, a mandatory filter guardrail must
be introduced to exclude non-completed invoices from all revenue metrics before any report is
published.

## Geographic attribution

Revenue is attributed to the customer's **billing country** (`BillingCountry`), not the
customer's registered country. These differ when a customer invoices to a different country than
their account address. Use `BillingCountry` for revenue-by-region reports and `customers.Country`
for customer-count or segmentation reports.

## Cross-system reconciliation

The canonical source for reconciliation is `chinook."Invoice"` on `chinook_pg`. Any discrepancy
with a payment processor or accounting system should be raised before publication. The
`Invoice.Total` column is the pre-computed sum of its `InvoiceLine` rows — if these diverge,
a data-integrity check is needed before either figure can be trusted.
