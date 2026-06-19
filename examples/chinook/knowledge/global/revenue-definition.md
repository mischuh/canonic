---
summary: "What total_revenue means and how it is calculated in the Chinook music store."
tags: [revenue, definitions, metrics]
sl_refs:
  - chinook_pg.invoices.total_revenue
  - chinook_pg.invoices
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-18T00:00:00Z"
---

**Total revenue** is the sum of all invoice totals — the amount each customer paid across every
completed purchase transaction.

The live expression — rendered directly from the semantic layer, so this definition can never drift:

> `{{ sl:chinook_pg.invoices.total_revenue.expr }}`

## What it includes

- Every row in `chinook."Invoice"`, aggregated by `sum("Total")`.
- `Invoice.Total` is the pre-computed sum of all line items on that invoice (each line being
  `InvoiceLine.UnitPrice × InvoiceLine.Quantity`). It equals `line_revenue` when both are
  queried at the invoice grain — see [[invoice-line-fanout-caveat]] for the cross-grain trap.

## What it excludes

The Chinook schema has no status or refund flag on invoices. All invoices in the dataset
represent completed purchases. If your deployment adds a cancellation mechanism, a guardrail
should be added to exclude cancelled invoices before publishing revenue figures.

## Grain

One row per `InvoiceId`. The measure is fully additive across all dimensions declared on
`invoices`: `billing_country`, `invoice_date`, `customer_id`, and any dimension reachable via
the join to `customers` (country, city, company).

## Currency

All amounts are stored in a single currency (USD in the standard Chinook dataset). No FX
conversion is applied. If your deployment stores multi-currency invoices, `Total` must be
normalised to a base currency before aggregating.
