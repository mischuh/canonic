# Canonic ecommerce demo

A small but end-to-end canonic project: a Postgres connection, a four-source star schema (two facts, three dimensions, plus an intraday `orders_rt` mirror), three canonical metrics, three guardrail contracts (a refund filter and a finality-backed board-reporting restriction), and a companion dbt manifest demonstrating the definition-connector class. The broadest walkthrough of the full loop: bootstrap, serve, evidence connectors, accuracy tracking, and observability all in one place.

Full walkthrough — MCP client config, evidence connectors (dbt/Notion/Metabase/Looker), accuracy tracking, observability, air-gapped mode, knowledge pages: **[`docs/guides/ecommerce.mdx`](../../docs/guides/ecommerce.mdx)**.

## Prerequisites

- Python ≥ 3.13, Canonic installed (`pip install -e ../..` from this directory)
- A Postgres database you can write to (local Docker, Neon free tier, etc.)

## Quickstart

```sh
export CANONIC_PG_PASSWORD=postgres   # password for the postgres user
psql "postgres://postgres:${CANONIC_PG_PASSWORD}@localhost:5432/postgres" < setup.sql

cd examples/ecommerce   # canonic commands must run from here
canonic status
canonic ingest --bootstrap
canonic query --metrics revenue --dimensions order_date
canonic mcp start
canonic eval baseline \   # optional: grain-inference accuracy
  --candidates candidates.yaml \
  --dataset eval/grain_cases.jsonl
```

`setup.sql` is idempotent; re-running it drops and recreates all tables in the correct order.

## What's in here

```
canonic.yaml                              ← project config + Postgres connection + dbt connection
setup.sql                                 ← CREATE TABLE + seed data
dbt/manifest.json                         ← dbt manifest mirroring the star schema; runs offline
candidates.yaml                           ← local model candidates for canonic eval baseline
eval/grain_cases.jsonl                    ← labeled grain-inference cases
semantics/warehouse_pg/                   ← orders, orders_rt, order_items, customers, products, channels
contracts/metrics/                        ← revenue, order-count, units-sold
contracts/guardrails/                     ← revenue-excludes-refunds, board-reporting-final-only,
                                             finality-revenue
knowledge/global/                         ← 5 definition/caveat/policy pages
docs/notion-pages/                        ← sample Notion page sources for the DocEvidence connector
```
