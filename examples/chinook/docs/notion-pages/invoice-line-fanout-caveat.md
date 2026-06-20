---
# Notion page properties:
#   "Canon Type"   (select):       caveat
#   "Canon Topics" (multi-select): invoice_lines, fanout, tracks_sold, line_revenue
canon_type: caveat
canon_topics: [invoice_lines, fanout, tracks_sold, line_revenue]
---

# Invoice Line Fanout Trap

`invoice_lines` sits at a **finer grain** than `invoices` — one invoice has multiple line items.
If you join the two tables and aggregate an invoice-level measure (`Invoice.Total`), each
invoice's total gets counted once per line item it contains.

An invoice with 5 tracks contributes its `Total` **5 times**. The result looks reasonable
but is wrong.

## What is safe

- Query `tracks_sold` or `line_revenue` from `invoice_lines` alone (or via its declared joins to
  `tracks` and `invoices`). The compiler enforces the `many_to_one` grain.
- Query `total_revenue` or `invoice_count` from `invoices` alone (or via its declared joins to
  `customers`).

## What is not supported in a single query

Querying `total_revenue` (from `invoices`) and `tracks_sold` (from `invoice_lines`) in the
**same** `query()` call crosses fact tables at different grains. This is blocked in Phase 1.

If you need both figures in one report: run two separate queries and join on `InvoiceId` in
your BI layer.

## Reconciliation check

`line_revenue` and `total_revenue` should agree when compared at the invoice grain.
`Invoice.Total` is the pre-computed sum of its line items, so the two measures represent the
same economic event from different grains. A discrepancy signals either a data-integrity issue
or an accidental cross-fact join.

## How this becomes a Canon knowledge page

Canon ingests this page as `DocEvidence` with `usage_hint: caveat`. E6 auto-surfaces it
whenever `invoice_lines.tracks_sold` or `invoice_lines.line_revenue` appears in a search result.
