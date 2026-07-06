---
# Notion page properties:
#   "Canonic Type"   (select):       definition
#   "Canonic Topics" (multi-select): units_sold, metrics, definitions, order_items
canonic_type: definition
canonic_topics: [units_sold, metrics, definitions, order_items]
---

# Units Sold Definition

**Units sold** is the total number of product units shipped across all non-refunded order line items.

## How it is calculated

Each order line item records the `quantity` of one product in one order. `units_sold` sums
that quantity across all line items belonging to non-refunded orders.

An order with two line items: 3 units of product A and 2 units of product B: contributes
**5** to `units_sold`.

## Grain

One row per **order line item**, not per order. The measure is fully additive across all
dimensions reachable from `order_items`:

- Product category (direct join to `products`)
- Sales channel (via `order_items → orders → channels`)
- Customer country (via `order_items → orders → customers`)

## What it excludes

Line items belonging to refunded orders. The `revenue-excludes-refunds` guardrail propagates
through joins, so refunded orders are excluded even when the query reaches `order_items`
through the join chain.

## How this differs from order_count

`order_count` counts distinct orders (one row per order). `units_sold` counts the total
number of items purchased across all orders. A single order with 5 line items contributes
**1** to `order_count` and **5+** to `units_sold`.

## How this becomes a Canonic knowledge page

Canonic ingests this page as `DocEvidence` with `usage_hint: definition`. E6 writes it as a
`definition`-mode knowledge page and surfaces it in `search_knowledge("units sold")`.
