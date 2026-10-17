---
summary: "What active_customers means and how it is calculated."
tags: [active_customers, definitions, metrics, rentals]
sl_refs:
  - rental_db.rentals
  - rental_db.rentals.customer_id
  - rental_db.rentals.status
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-10-16T00:00:00Z"
---

**Active customers** is the count of distinct customers who have at least one non-cancelled rental.

The metric is a `distinct_count` binding on `rentals.customer_id` with the population filter
`status != 'cancelled'`. A distinct count has no measure expression to render, so the definition
lives in the metric contract.

## Grain

One unique customer per distinct `customer_id`. The metric can be sliced by any dimension
reachable from `rentals` (vehicle category, pickup location, membership tier, rental date), but
a distinct count does not add up across slices. A customer who rents in two locations counts once
in each.

## What it counts

Each row in `rentals` represents one rental transaction. `active_customers` counts the
distinct `customer_id` values where the rental status is anything other than `'cancelled'`.
A customer with 5 completed rentals, 2 pending rentals, and 1 cancelled rental contributes
1 to `active_customers`.

## What it excludes

Rentals with `status = 'cancelled'` are excluded from the distinct count. This means
customers with only cancelled rentals are not counted as active customers.

## How it differs from related measures

| Measure | Source | Counts |
|---|---|---|
| `active_customers` | rentals | distinct customers with at least one non-cancelled rental |
| `rental_count` | rentals | total number of rental transactions (all statuses) |

## Typical values

In the rental demo data, active customers range from 15–25 depending on the reporting period
and membership tier filters applied.
