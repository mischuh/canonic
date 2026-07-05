---
summary: "Geometry columns are plain WKT text, not queryable shapes."
tags: [geography, geometry]
sl_refs:
  - railway_duckdb.dim_nl_provinces
  - railway_duckdb.dim_nl_municipalities
  - railway_duckdb.dim_nl_train_stations
usage_mode: reference
meta:
  provenance: human_curated
  last_validated_at: "2026-07-05T00:00:00Z"
---

Upstream's dbt project resolves station → municipality → province with real polygon geometry
(DuckDB's `spatial` extension: `st_contains`, `st_covers`, `st_area`). Canonic has no native
geometry type, and the resolved joins already live in `*_sk` surrogate keys — so this example
converts geometry to plain text at build time and drops the extension dependency entirely:

- `province_centroid_wkt` / `municipality_centroid_wkt` — the polygon's centroid, as
  `POINT (lng lat)` WKT text (not the full boundary — a province polygon's WKT is tens of KB,
  far too large to be a useful column value).
- `station_location_wkt` — the station's exact point, as `POINT (lng lat)` WKT text.

These columns are informational only — group by `province_name` / `station_name`, not by the
WKT text.
