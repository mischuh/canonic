---
# Notion page properties:
#   "Canon Type"   (select):       definition
#   "Canon Topics" (multi-select): revenue, metrics, definitions
canon_type: definition
canon_topics: [revenue, metrics, definitions]
---

# Revenue Definition

**Total revenue** is the sum of all invoice totals — the amount each customer paid across every
completed purchase transaction in the music store.

## How it is calculated

Each row in the `Invoice` table carries a pre-computed `Total` that equals the sum of its line
items (`InvoiceLine.UnitPrice × InvoiceLine.Quantity`). `total_revenue` aggregates this column:
`sum("Total")`.

## Grain

One row per invoice (`InvoiceId`). The metric is fully additive across all dimensions on
`invoices`: billing country, invoice date, customer, and any dimension reachable via the join
to `customers` (city, company, support representative).

## What it includes

All invoices in the Chinook dataset represent completed purchases. The schema has no cancellation
or refund flag, so every invoice is included.

> If your deployment adds a cancellation mechanism, add a guardrail to exclude cancelled invoices
> before publishing revenue figures. See the `revenue-excludes-refunds` guardrail in the ecommerce
> demo for a reference implementation.

## Currency

All amounts are stored in a single currency (USD in the standard Chinook dataset). No FX
conversion is applied. If your deployment stores multi-currency invoices, `Invoice.Total` must
be normalised to a base currency before aggregating.

## How this becomes a Canon knowledge page

Canon ingests this page as `DocEvidence` with `usage_hint: definition`. E6 writes it as a
`definition`-mode knowledge page, surfaced by `search_knowledge("revenue definition")`.
