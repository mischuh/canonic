---
summary: "Why dim_nl_provinces, dim_nl_municipalities, and dim_nl_train_stations each carry a synthetic 'unknown' row."
tags: [geography, joins, data-quality]
sl_refs:
  - railway_duckdb.dim_nl_municipalities
  - railway_duckdb.dim_nl_train_stations
usage_mode: caveat
meta:
  provenance: human_curated
  last_validated_at: "2026-07-05T00:00:00Z"
---

Municipality and province boundaries are resolved by a spatial point-in-polygon join at build
time (`st_contains` / `st_covers`), not a foreign key in the source data. A station whose
coordinates fall outside every province polygon (e.g. right at the coast, or just over a border)
has no match — it gets `municipality_sk = 'unknown'`, which cascades to `province_sk = 'unknown'`
on `dim_nl_provinces`.

Grouping `service_count` by `province_name` therefore always has a small `unknown` bucket. This
is expected, not a data-quality bug — treat it as "location not resolved", not "zero".
