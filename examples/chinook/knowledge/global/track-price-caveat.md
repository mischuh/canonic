---
summary: "Track.UnitPrice is the current list price; InvoiceLine.UnitPrice is what the customer actually paid."
tags: [revenue, caveats, pricing, tracks, invoice_lines]
sl_refs:
  - chinook_pg.invoice_lines.line_revenue
  - chinook_pg.tracks
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-18T00:00:00Z"
---

## Two prices, different meanings

The Chinook schema stores unit price in two places:

| Column | Meaning |
|---|---|
| `chinook."Track"."UnitPrice"` | Current **list price** — what a customer would pay today |
| `chinook."InvoiceLine"."UnitPrice"` | **Sale price** — what the customer actually paid at the time of purchase |

These differ whenever a track's list price changes after a sale. Using `Track.UnitPrice` to
reconstruct historical revenue will produce figures that **do not match actual receipts**.

## Why this matters

The `use-sale-price-not-list-price` guardrail (`severity: warning`) fires when a query
references `Track.UnitPrice` in the context of revenue calculations. The correct expression
for revenue is always `sum("InvoiceLine"."UnitPrice" * "InvoiceLine"."Quantity")`, which is
what the `line_revenue` measure computes.

## When Track.UnitPrice is valid

Use `Track.UnitPrice` only for **catalogue pricing queries**: "what does this track cost right
now?", "how many tracks are priced at $0.99 vs $1.29?". Never aggregate it as revenue.

## In the standard Chinook dataset

The standard dataset has two list prices: **$0.99** (standard tracks) and **$1.29** (HD video).
All invoice lines also carry one of these two prices, so in the seed data list price and sale
price happen to match. The guardrail exists because this equality breaks as soon as prices are
updated in a real deployment.
