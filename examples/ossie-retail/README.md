# Canonic Ossie retail demo

A canonic project bootstrapped from an [Apache Ossie](https://ossie.apache.org/) semantic model: one SQLite connection for a small coffee retailer, plus one `ossie` connection that reads `ossie/retail.ossie.yaml`. Every file under `semantics/`, `contracts/` and `knowledge/` was drafted by `canonic ingest` from that model and accepted with `canonic apply`. Nothing is hand-written.

Full walkthrough, mapping table and the CI gate: **[`docs/guides/ossie-retail.mdx`](../../docs/guides/ossie-retail.mdx)**.

## Prerequisites

- Python ≥ 3.13, Canonic installed (`pip install -e ../..` from this directory)
- `sqlite3` to create the database from `setup.sql`
- No LLM key needed. Ossie parsing is deterministic and every table has a primary key.

## Quickstart

```sh
cd examples/ossie-retail          # canonic commands must run from here
sqlite3 retail.db < setup.sql     # create the database (one-time)
canonic connection test           # the ossie connection reports its unmappable objects
canonic query --metrics revenue --dimensions region
canonic query --metrics average_order_value --dimensions channel
canonic mcp start
```

## Rebuild the context from the Ossie model

```sh
rm -rf semantics contracts knowledge raw-sources .canonic
canonic ingest --bootstrap --headless       # writes the unambiguous semantic sources
canonic ingest --headless                   # proposes the rest for review
canonic review                              # or accept everything: canonic apply .canonic/pending-diffs/<run>
```

## What's in here

```
canonic.yaml                 ← SQLite connection + ossie connection (target_connection: retail_db)
setup.sql                    ← DDL + seed data: 6 orders, 9 order lines, revenue 284.00
ossie/retail.ossie.yaml      ← the Ossie model, every mapping path appears once
semantics/retail_db/         ← 6 sources: dimensions, aliases and joins from Ossie
contracts/metrics/           ← 4 bindings from Ossie metrics (single, distinct_count, ratio)
knowledge/global/            ← 6 pages from Ossie ai_context instructions
raw-sources/                 ← evidence snapshots of the last ingest run
```

Three objects in the model are intentionally not imported, to show how the connector reports them: the query-sourced dataset `recent_orders`, the MDX-only metric `revenue_cube`, and the window function `running_revenue`.
