---
summary: "How avg_rental_duration is computed and when planned_days differs from actual_days."
tags: [avg_rental_duration, duration, definitions, rentals]
sl_refs:
  - rental_db.rentals.avg_rental_days
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-06-22T00:00:00Z"
---

**Average rental duration** is the mean number of days a vehicle was actually held, computed
across completed rentals only.

The live expression:

> `{{ sl:rental_db.rentals.avg_rental_days.expr }}`

## planned_days vs actual_days

| Column | Meaning | Populated |
|---|---|---|
| `planned_days` | Days requested at booking time | Always |
| `actual_days` | Days from pickup to real dropoff | Completed rentals only |

`actual_days` is NULL for active, confirmed, cancelled, and no-show rentals.
`avg_rental_days` averages `actual_days`, so it automatically excludes non-completed rows
(SQLite `avg()` ignores NULLs). No additional status filter is required for this measure —
but be explicit when grouping, to avoid surprising segment totals that mix completed and
active rows.

## Early returns and extensions

Customers occasionally return a vehicle early (actual < planned) or extend their booking
(actual > planned). `avg_rental_days` reflects the real fleet utilisation, not the booked
demand. To compare demand vs utilisation, query both columns in the same select:

```sql
select avg(planned_days) as avg_planned, avg(actual_days) as avg_actual
from rentals
where status = 'completed';
```
