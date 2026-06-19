---
summary: "Joining invoices to invoice_lines inflates invoice-level metrics — the classic fanout trap."
tags: [invoice_lines, caveats, fanout, line_revenue, tracks_sold]
sl_refs:
  - chinook_pg.invoice_lines.tracks_sold
  - chinook_pg.invoice_lines.line_revenue
  - chinook_pg.invoice_lines
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-18T00:00:00Z"
---

## The fanout problem

`invoice_lines` is at a **finer grain** than `invoices` — multiple line items share the same
`InvoiceId`. If you join the two tables and then aggregate `invoices.Total`, each invoice's
total gets duplicated once per line item. An invoice with 5 tracks contributes its total 5 times.

**Safe:** query `tracks_sold` or `line_revenue` from `invoice_lines` alone (or via its declared
joins to `tracks` and `invoices`). The compiler enforces the declared `many_to_one` grain.

**Unsafe:** query `total_revenue` (from `invoices`) and `tracks_sold` (from `invoice_lines`)
together in a single `query()` call — this crosses fact tables at different grains.
Use two separate queries and join on `InvoiceId` in your BI layer if you need both in one view.

## Reconciliation check

`line_revenue` and `total_revenue` should agree when compared at the invoice grain.
`Invoice.Total` is the pre-computed sum of its line items, so the two measures are definitionally
equal at that grain. A discrepancy signals either a data-integrity issue (Invoice.Total was not
updated after a line item change) or an inadvertent cross-grain join inflating one side.

## Playlist fanout

A second, less obvious fanout: if you join `tracks` → `PlaylistTrack` to find which playlists
contain purchased tracks, you will multiply each line item by the number of playlists the track
belongs to. The `no-playlist-join-in-revenue` guardrail blocks this automatically.
See [[track-price-caveat]] for the related list-price vs sale-price distinction.
