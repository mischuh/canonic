---
summary: "What tracks_sold means and how it differs from invoice_count and line_count."
tags: [tracks_sold, definitions, metrics, invoice_lines]
sl_refs:
  - chinook_pg.invoice_lines.tracks_sold
  - chinook_pg.invoice_lines
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-18T00:00:00Z"
---

**Tracks sold** is the total number of individual track downloads across all invoice line items.

The live expression — rendered directly from the semantic layer:

> `{{ sl:chinook_pg.invoice_lines.tracks_sold.expr }}`

## What it counts

Each `InvoiceLine` row records the purchase of one track (`TrackId`) at a specific `Quantity`.
`tracks_sold` sums that quantity. In the standard Chinook dataset every line has `Quantity = 1`,
so `tracks_sold` equals `line_count` — but the measure is defined as `sum("Quantity")` to remain
correct if bulk-download quantities are ever introduced.

## How it differs from related metrics

| Metric | Grain | Counts |
|---|---|---|
| `invoice_count` | invoice | distinct customer transactions |
| `line_count` | invoice_line | distinct track-in-invoice pairings |
| `tracks_sold` | invoice_line | total quantity of tracks downloaded |

## Grain

One row per `InvoiceLineId`. The measure is fully additive across all dimensions reachable from
`invoice_lines` — genre and media type (via `invoice_lines → tracks → genres / media_types`),
artist and album (via `invoice_lines → tracks → albums → artists`), and billing country
(via `invoice_lines → invoices → customers`).

## Fanout warning

See [[invoice-line-fanout-caveat]] before joining `invoice_lines` back to `invoices` in the
same query. Querying `tracks_sold` and `total_revenue` together crosses two fact sources and
is not supported in a single `query()` call.
