# Canonic Dutch Railway Network demo

A DuckDB-native Canonic example, ported from DuckDB's own blog example
[`duckdb-blog-examples/dbt_duckdb/dutch_railway_network`](https://github.com/duckdb/duckdb-blog-examples/tree/main/dbt_duckdb/dutch_railway_network).
No live database, no credentials — a single committed `.duckdb` file plus a companion dbt
manifest. It's a different schema *shape* than [examples/ecommerce/](../ecommerce/): a
geography dimension chain (station → municipality → province) resolved by spatial joins at
build time, an SCD2-flavored dimension with a synthetic `unknown` fallback row, and a fact
table pinned to a single demo day instead of ecommerce's tiny hand-seeded rows.

## Phase 1 loop

```
canonic ingest --bootstrap          # 1. bootstrap: introspect DuckDB → write semantics/*.yaml
canonic mcp start                   # 2. serve: agents call query() + search_knowledge() together
```

## What's in here

```
canonic.yaml                              ← project config + DuckDB connection + dbt connection
dutch_railway.duckdb                      ← prebuilt, self-contained warehouse (~4MB, no extensions needed)
dbt/manifest.json                         ← E3 definition connector: compiled dbt manifest (schema v12)
semantics/railway_duckdb/
  dim_nl_provinces.yaml                   ← dim: 12 provinces + 1 'unknown' fallback row
  dim_nl_municipalities.yaml              ← dim: 342 municipalities + 1 'unknown', joins to provinces
  dim_nl_train_stations.yaml              ← dim: 397 NL stations, joins to municipalities
  fact_services.yaml                      ← fact: service_count/scheduled_stop_count/cancelled_*_count, joins to stations
contracts/metrics/
  service-count.yaml                      ← kind: single  — service_count → fact_services.service_count (guardrailed)
  scheduled-stops.yaml                    ← kind: single  — helper: ungated denominator for the rate metrics below
  cancelled-arrivals.yaml                 ← kind: single  — cancelled_arrivals → fact_services.cancelled_arrival_count
  cancelled-departures.yaml               ← kind: single  — cancelled_departures → fact_services.cancelled_departure_count
  cancelled-arrival-rate.yaml             ← kind: ratio    — cancelled_arrivals / scheduled_stops
  cancelled-departure-rate.yaml           ← kind: ratio    — cancelled_departures / scheduled_stops
contracts/guardrails/
  exclude-cancelled-arrivals.yaml         ← mandatory_filter: service_arrival_cancelled = false (on service_count only)
knowledge/global/
  unknown-fallback-rows.md                ← usage_mode: caveat — why the 'unknown' geography bucket exists
  geometry-as-wkt.md                      ← usage_mode: reference — why geometry columns are WKT text, not shapes
scripts/
  build.sh                                ← regenerates dutch_railway.duckdb + dbt/manifest.json from upstream
  postprocess.py                          ← the two build-only fixups build.sh applies (see below)
  dbt_project/                            ← trimmed dbt-duckdb project (transformation models only)
```

## Prerequisites

- Python ≥ 3.13, Canonic installed (`pip install -e ../..` from this directory)
- Nothing else — DuckDB is a local file, no server, no credentials.

## Setup

```sh
cd examples/dutch-railway   # ← must run canonic commands from here
canonic status
# Canonic project: dutch-railway-demo (version 1)
```

## Data

The fact table is pinned to a **single demo day** (`service_date = 2024-08-01`) rather than
upstream's full multi-year history (~22M rows) — 63,946 service stops across 397 stations,
matching the "small but real" seed convention used by `examples/ecommerce/`.

| Table | Rows | Grain |
| --- | --- | --- |
| `dim_nl_provinces` | 13 (12 + `unknown`) | `province_sk` |
| `dim_nl_municipalities` | 343 (342 + `unknown`) | `municipality_sk` |
| `dim_nl_train_stations` | 397 | `station_sk` |
| `fact_services` | 63,946 | `service_sk` |
| `ams_traffic_v` (view) | 1,184 | — Amsterdam Centraal only |

## Example tool calls

**List canonical metrics:**
```json
list_metrics()
→ [
    {"metric": "service_count",            "source": "fact_services", "measure": "service_count"},
    {"metric": "scheduled_stops",           "source": "fact_services", "measure": "scheduled_stop_count"},
    {"metric": "cancelled_arrivals",        "source": "fact_services", "measure": "cancelled_arrival_count"},
    {"metric": "cancelled_departures",      "source": "fact_services", "measure": "cancelled_departure_count"},
    {"metric": "cancelled_arrival_rate",    "kind": "ratio", "numerator": "cancelled_arrivals",   "denominator": "scheduled_stops"},
    {"metric": "cancelled_departure_rate",  "kind": "ratio", "numerator": "cancelled_departures", "denominator": "scheduled_stops"}
  ]
```

**Services by station (guardrail fires automatically):**
```json
query({"metrics": ["service_count"], "dimensions": ["station_name"]})
```

**Services by province — exercises the three-hop join
`fact_services → dim_nl_train_stations → dim_nl_municipalities → dim_nl_provinces`:**
```json
query({"metrics": ["service_count"], "dimensions": ["province_name"]})
```

**Cancelled arrivals — the sibling metric, not filtered by the guardrail (it applies only to
`service_count`):**
```json
query({"metrics": ["cancelled_arrivals"]})
→ {"result": {"columns": [...], "rows": [[5644]]}}
```

**Arrival cancellation rate — a `kind: ratio` metric. Ratio/weighted_avg metrics compile to a
CTE per component and must be queried alone (no other metrics or dimensions in the same call):**
```json
query({"metrics": ["cancelled_arrival_rate"]})
→ {"result": {"columns": [{"name": "cancelled_arrival_rate", "type": "float"}], "rows": [[0.0883]]}}
```

## What the guardrail does

`exclude-cancelled-arrivals` is a `mandatory_filter` scoped to `fact_services.service_count`.
Every query on `service_count` gets `service_arrival_cancelled = false` AND-ed into the WHERE
clause automatically — a cancelled arrival never actually happened at the station, so it
shouldn't count as a realized stop. `cancelled_arrivals` is a separate metric, unaffected by
the guardrail (it exists specifically to count what the other one excludes).

`scheduled_stops` (`scheduled_stop_count`) has the *same expr* as `service_count`
(`count(service_sk)`) under a different measure name, so the guardrail — which matches by
measure name — does not touch it. That's deliberate: `cancelled_arrival_rate` needs an
**unguarded** total in its denominator (all scheduled stops, cancelled or not); reusing the
guarded `service_count` there would silently exclude cancellations from both sides of the
ratio and always yield the same value regardless of cancellations.

## E3 connector — dbt manifest

Wired into [`canonic.yaml`](canonic.yaml) exactly like `examples/ecommerce/`'s dbt connection —
no database, no credentials, offline:

```yaml
- id: railway_dbt
  type: dbt
  params:
    manifest_path: dbt/manifest.json
```

```sh
canonic ingest --connection railway_dbt --dry-run
```

Upstream's dbt project has no MetricFlow `semantic_models`/`metrics` blocks — it's a
dbt-duckdb engineering post, not a semantics one — so the manifest contributes `RelationSchema`
+ `MODEL` evidence (descriptions, grain, FK joins) rather than measure/dimension evidence. The
real measures live in the hand-authored `semantics/railway_duckdb/*.yaml`, same as ecommerce's
E3 story: modeling-tier evidence from dbt still outranks live DuckDB introspection where they
overlap (SPEC-E3 §6).

## What we ported, and what we didn't

Upstream's project has three model layers: `transformation/` (the star schema below),
`reverse_etl/` (writes copies back into Postgres), and `exports/` (writes GeoJSON/Parquet
files for charts). We port only `transformation/` — the other two are dbt-duckdb engineering
demos (cross-database attach, file export) that need a live Postgres and add no semantic-layer
value. See [`scripts/build.sh`](scripts/build.sh) for the exact regen recipe, including two
build-only fixups `postprocess.py` applies and why:

1. **Geometry → WKT text.** Upstream resolves station→municipality→province with real
   polygon geometry (DuckDB's `spatial` extension). Canonic has no geometry type and the
   joins already live in `*_sk` surrogate keys, so geometry becomes plain WKT text at build
   time — a **centroid** point for the two polygon dimensions (a province boundary's WKT is
   tens of KB; a centroid is one `POINT (lng lat)`), the exact point for stations. See
   [`knowledge/global/geometry-as-wkt.md`](knowledge/global/geometry-as-wkt.md).
2. **dbt manifest relation names.** dbt-duckdb's compiled manifest tags every model with
   `database: <catalog>`, but Canonic's `DuckDBConnector` introspects relations as
   `main.<table>` (no catalog prefix). Left alone, the two tiers would never line up during
   reconciliation, so the build nulls `database` on every model node — the same fix
   `examples/ecommerce/dbt/manifest.json` already carries for its Postgres models.

## Regenerating the artifacts

```sh
cd examples/dutch-railway/scripts
./build.sh
```

Needs network access (attaches a ~400MB remote DuckDB file over `httpfs`, streamed not
downloaded in full, plus a remote GeoJSON fetch) and `uv`. Builds an isolated dbt-duckdb venv,
runs `dbt build --select transformation` against [`scripts/dbt_project/`](scripts/dbt_project/),
then overwrites `dutch_railway.duckdb` and `dbt/manifest.json` in place.
