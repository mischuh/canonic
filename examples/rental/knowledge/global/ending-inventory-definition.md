---
summary: "What ending_inventory means and how it is calculated as a point-in-time snapshot."
tags: [ending_inventory, inventory, fleet_size, definitions, metrics, vehicle_inventory]
sl_refs:
  - rental_db.vehicle_inventory.ending_inventory
  - rental_db.vehicle_inventory.inventory_level
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-24T00:00:00Z"
---

**Ending inventory** is the count of available vehicles at the last snapshot date of a reporting period,
measured per location and vehicle category. It represents a point-in-time fleet size, not a flow.

The live expression: rendered directly from the semantic layer:

> `{{ sl:rental_db.vehicle_inventory.ending_inventory.expr }}`

## Grain

One snapshot per unique combination of `vehicle_id`, `location_id`, and `snapshot_date`. The measure is
fully additive **across vehicles and locations** within a single date, but **NOT additive over time**; it
is a stock, not a flow.

## What it counts

Each row in the `vehicle_inventory_snapshots` table records the count of available vehicles for a vehicle
category at a specific location on a specific date. `ending_inventory` sums the `inventory_level` across
all vehicles and locations, then takes the **last snapshot** in the reporting period. For a month-long
query, only the snapshot from the last day of that month is retained and summed.

## Semi-additive semantics

This measure is **semi-additive**. You can safely sum it across:
- Different vehicles and vehicle categories
- Different pickup/dropoff locations
- Different membership tiers or other dimensions joined through vehicles

You **cannot** sum it across time periods. Doing so yields meaningless results (e.g., summing ending
inventory from June 30 and July 31 does not equal August 31 inventory). Always filter or pivot by date
to analyze inventory trends.

## What it excludes

- Vehicles not yet added to inventory management
- Vehicles retired from service (not in snapshots)
- Damaged vehicles awaiting repair (counted as on-hand; captured in damage records separately)
- On-rent vehicles (counted as unavailable in the snapshot)

## How it differs from related measures

| Measure | Type | Meaning |
|---|---|---|
| `ending_inventory` | Stock (snapshot) | Fleet size on the last day of a period |
| `inventory_level` | Stock (snapshot) | Inventory count on any snapshot date |
| `rental_count` | Flow | Number of rental transactions (additive over time) |
| `active_customers` | Distinct count | Unique customers with non-cancelled rentals |

## Typical usage

Ending inventory is best used for:
- Fleet planning and capacity analysis ("What's our vehicle count by location?")
- Period-end reporting ("How many cars were on hand at month-end?")
- Slicing by location, category, or membership patterns across a fixed date

Avoid using it in:
- Period-over-period trend analysis without careful date filtering
- Queries summing multiple time periods
- Correlations with flow measures (rental revenue, damage counts) without separate aggregation

## Typical values

In the rental demo data, ending inventory per location ranges from 3–5 vehicles depending on the snapshot
date and location utilization.
