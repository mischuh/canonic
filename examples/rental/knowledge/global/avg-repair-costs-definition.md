---
summary: "What avg_repair_costs means, how it is calculated, and its scope."
tags: [avg_repair_costs, repair_costs, definitions, metrics, damages]
sl_refs:
  - rental_db.damages.avg_repair_costs
  - rental_db.damages.total_repair_cost
  - rental_db.damages.damage_count
usage_mode: definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-24T00:00:00Z"
---

**Average repair costs** is the mean cost per vehicle damage incident, calculated as
total repair cost divided by damage count, filtered to moderate and major severity damages only.

The live expression — rendered directly from the semantic layer:

> `{{ sl:rental_db.damages.avg_repair_costs.expr }}`

## What it counts

Each row in `damages` represents one recorded vehicle damage incident. `avg_repair_costs`
sums the `repair_cost` of all damages with `severity IN ('major', 'moderate')`, then divides
by the count of such damage records. A rental with 3 recorded damages (2 major at $500 each,
1 minor at $50) that passes the filter contributes $500 to the numerator per major damage,
but the minor damage is excluded.

## Grain

One row per `damage_id`. The measure is fully additive across vehicle category, rental
location, and damage date, but only among damages matching the severity filter.

## What it excludes

| Excluded | Reason |
|---|---|
| Minor severity damages | Only major and moderate damages are included |
| Cosmetic damages | Not recorded in the damages table |
| Pending repairs | Repair costs reflect invoiced amounts; pending/estimated work is not counted |

## Population filter

The filter `severity IN ('major', 'moderate')` is enforced by the canonical metric definition.
This ensures that repair cost analysis focuses on significant damage events and excludes
wear-and-tear or minor cosmetic issues.

## Zero denominator handling

When a reporting period has no major or moderate damages, the measure returns `null`
rather than 0, preventing misleading zero-cost interpretations.

## How it differs from related measures

| Measure | Source | Calculates |
|---|---|---|
| `avg_repair_costs` | damages | average cost per damage incident (major/moderate only) |
| `total_repair_cost` | damages | sum of all repair costs (before severity filter) |
| `rental_revenue` | payments | rental income — separate from repair/damage costs |

## Typical values

In the rental demo data, average repair costs for major and moderate damages range from
$250–$800 depending on vehicle category and time period.
