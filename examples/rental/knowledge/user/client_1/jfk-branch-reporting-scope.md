---
summary: "Personal note for client_1: default rental_revenue and duration reporting to the JFK / New York branch unless another location is asked for."
tags: [jfk, new-york, branch, personal, rental_revenue]
sl_refs:
  - rental_db.payments.total_paid
  - rental_db.locations.city
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-07-12T00:00:00Z"
---

**Personal reporting default.** I run the JFK / New York branch (`locations.location_id = 1`,
`code = 'JFK-LOC'`). Unless a question names a different city, assume it's about this branch
and filter `rentals.pickup_location_id = 1` (or join through `pickup` to
`locations.city = 'New York'`).

See [[rental-revenue-definition]] for what `rental_revenue` counts globally: this note only
adds the location default, it doesn't change the metric itself.

## Why this matters for me

Company-wide `rental_revenue` and `avg_rental_duration` figures mix all five branches. For my
own weekly review I only care about JFK, so any answer that isn't already scoped to JFK needs a
location filter added before I can use the number.

## Quick filter

```sql
select *
from rentals r
join locations l on l.location_id = r.pickup_location_id
where l.code = 'JFK-LOC';
```
