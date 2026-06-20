---
# Notion page properties:
#   "Canon Type"   (select):       definition
#   "Canon Topics" (multi-select): tracks_sold, metrics, definitions, invoice_lines
canon_type: definition
canon_topics: [tracks_sold, metrics, definitions, invoice_lines]
---

# Tracks Sold Definition

**Tracks sold** is the total number of individual track downloads across all invoice line items.

## How it is calculated

Each `InvoiceLine` records the purchase of one track at a specific `Quantity`. `tracks_sold`
is `sum("Quantity")` across all line items.

In the standard Chinook dataset every line item has `Quantity = 1`, so `tracks_sold` equals
`line_count`. The measure is defined as `sum("Quantity")` — rather than `count(*)` — so it
remains correct if bulk-download quantities are ever introduced.

## How it differs from related metrics

| Metric | Grain | What it counts |
|---|---|---|
| `invoice_count` | invoice | distinct customer transactions |
| `line_count` | invoice line | distinct track-in-invoice pairings |
| `tracks_sold` | invoice line | total quantity of tracks downloaded |

Use `tracks_sold` for catalogue performance analysis. Use `invoice_count` for transaction volume.

## Grain

One row per `InvoiceLineId`. The measure is fully additive across all dimensions reachable
from `invoice_lines`:

- Genre and media type (via `invoice_lines → tracks → genres / media_types`)
- Artist and album (via `invoice_lines → tracks → albums → artists`)
- Billing country (via `invoice_lines → invoices → customers`)

## Fanout warning

Do not query `tracks_sold` and `total_revenue` together in a single `query()` call. See the
*Invoice Line Fanout Trap* page for the reason.

## How this becomes a Canon knowledge page

Canon ingests this page as `DocEvidence` with `usage_hint: definition`. E6 writes it as a
`definition`-mode knowledge page, surfaced by `search_knowledge("tracks sold")`.
