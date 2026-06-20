---
# Notion page properties:
#   "Canon Type"   (select):       caveat
#   "Canon Topics" (multi-select): revenue, pricing, tracks, invoice_lines
canon_type: caveat
canon_topics: [revenue, pricing, tracks, invoice_lines]
---

# Track Price vs. Sale Price

The Chinook schema stores unit price in two places. **They have different meanings** and should
never be used interchangeably in revenue calculations.

| Column | Meaning |
|---|---|
| `Track.UnitPrice` | Current **list price** — what a customer would pay today |
| `InvoiceLine.UnitPrice` | **Sale price** — what the customer actually paid at purchase time |

These diverge whenever a track's list price is updated after a sale. Using `Track.UnitPrice` to
reconstruct historical revenue produces figures that **do not match actual receipts**.

## The right expression for revenue

Always use `InvoiceLine.UnitPrice` (multiplied by `InvoiceLine.Quantity`) for revenue
calculations. This is what the `line_revenue` measure computes. The `use-sale-price-not-list-price`
guardrail fires a warning when a query references `Track.UnitPrice` in a revenue context.

## When Track.UnitPrice is valid

`Track.UnitPrice` is correct for **catalogue pricing queries**: "what does this track cost right
now?", "how many tracks are priced at $0.99 vs $1.29?". Never aggregate it as revenue.

## In the standard Chinook dataset

The standard dataset has two list prices — **$0.99** (standard tracks) and **$1.29** (HD video).
All invoice lines also carry one of these two prices, so in the seed data list price and sale
price happen to agree. The guardrail exists because this equality breaks in any deployment where
prices are ever updated.

## How this becomes a Canon knowledge page

Canon ingests this page as `DocEvidence` with `usage_hint: caveat`. E6 auto-surfaces it
whenever a search result touches `invoice_lines.line_revenue` or `tracks`.
