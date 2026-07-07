# Canonic Dutch Railway Network demo

A DuckDB-native canonic example ported from [DuckDB's own blog example](https://github.com/duckdb/duckdb-blog-examples/tree/main/dbt_duckdb/dutch_railway_network): a geography dimension chain (station → municipality → province), a synthetic `unknown` fallback row, and a `mandatory_filter` guardrail paired with a deliberately unguarded sibling metric.

Full walkthrough, metrics table, and guardrail rationale: **[`docs/guides/dutch-railway.mdx`](../../docs/guides/dutch-railway.mdx)**.

## Prerequisites

- Python ≥ 3.13, Canonic installed (`pip install -e ../..` from this directory)
- Nothing else: `dutch_railway.duckdb` is a local file, no server, no credentials.

## Quickstart

```sh
cd examples/dutch-railway   # canonic commands must run from here
canonic status
canonic ingest --bootstrap
canonic query --metrics service_count --dimensions station_name
canonic mcp start
```

## What's in here

```
canonic.yaml                    ← project config + DuckDB connection + dbt connection
dutch_railway.duckdb            ← prebuilt, self-contained warehouse (~4MB, no extensions needed)
dbt/manifest.json               ← dbt manifest (schema v12), model/relation evidence only
semantics/railway_duckdb/       ← 3 dimensions (incl. 2 with an 'unknown' fallback row) + 1 fact
contracts/metrics/              ← 6 metric contracts (service_count, scheduled_stops,
                                   cancelled_arrivals/departures + their ratio metrics)
contracts/guardrails/           ← exclude-cancelled-arrivals.yaml
knowledge/global/                ← unknown-fallback-rows.md, geometry-as-wkt.md
scripts/                        ← build.sh + postprocess.py: regenerates the artifacts from upstream
```

## Regenerating the artifacts

```sh
cd examples/dutch-railway/scripts
./build.sh
```

Needs network access (attaches a remote DuckDB file over `httpfs`, plus a GeoJSON fetch) and `uv`. See [the guide](../../docs/guides/dutch-railway.mdx#what-got-adapted-from-upstream) for what the two build-time fixups (geometry → WKT, manifest relation names) do and why.
