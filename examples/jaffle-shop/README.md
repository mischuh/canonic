# Canonic Jaffle Shop demo

A canonic project backed by the classic [dbt Jaffle Shop](https://github.com/dbt-labs/jaffle-shop) dataset: a bundled DuckDB file plus a dbt manifest with MetricFlow semantic models, showing modeling-tier evidence outranking raw introspection.

Full walkthrough, metric catalogue, and explanation of what canonic extracts from the manifest: **[`docs/guides/jaffle-shop.mdx`](../../docs/guides/jaffle-shop.mdx)**.

## Prerequisites

- Python ≥ 3.13, Canonic installed (`pip install -e ../..` from this directory)
- Nothing else: `jaffle_shop.duckdb` is a local file, no server, no credentials.

## Quickstart

```sh
cd examples/jaffle-shop   # canonic commands must run from here
canonic status
canonic ingest --bootstrap
canonic query --metrics revenue --dimensions store_id
canonic mcp start
```

## What's in here

```
canonic.yaml                ← project config + DuckDB connection + dbt connection
jaffle_shop.duckdb          ← bundled pre-built database
dbt/manifest.json           ← dbt manifest (schema v11, dbt 1.7, MetricFlow)
semantics/jaffle_duckdb/    ← 5 relations, drafted from manifest + DuckDB introspection
contracts/metrics/          ← 8 metric contracts (revenue, order_count, units_sold + 5 helpers)
knowledge/global/           ← business definitions and caveats
scripts/build.sh            ← regenerates the .duckdb + manifest from upstream
```

## Regenerating the artifacts

```sh
bash scripts/build.sh
```

Clones upstream `dbt-labs/jaffle-shop`, runs `dbt build` with `dbt-duckdb`, and copies the resulting database and manifest back into this directory.
