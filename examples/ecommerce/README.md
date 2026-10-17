# Canonic ecommerce demo

A small but end-to-end canonic project: a Postgres connection, a six-source star schema (two facts, three dimensions, plus an intraday `orders_rt` mirror), three canonical metrics, four contract files (two refund filters, a board-reporting restriction and the finality rule behind it), three assertions, and a companion dbt manifest demonstrating the definition-connector class. The broadest walkthrough of the full loop: bootstrap, serve, evidence connectors, accuracy tracking, and observability all in one place.

Full walkthrough: MCP client config, evidence connectors (dbt/Notion/Metabase/Looker), accuracy tracking, observability, air-gapped mode, knowledge pages. **[`docs/guides/ecommerce.mdx`](../../docs/guides/ecommerce.mdx)**.

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

`setup.sql` is idempotent. Re-running it drops and recreates all tables in the correct order.

## Evidence from Notion docs

`docs/notion-pages/` shows the page format the Notion connector expects: the two page
properties it reads deterministically, no LLM involved (`Canonic Type` → `usage_hint`,
`Canonic Topics` → candidate `topic_refs`). Wiring it into a real workspace is a connection
in `canonic.yaml`:

```yaml
connections:
  - id: handbook_notion
    type: notion
    credentials_ref: env:NOTION_TOKEN
```

`canonic ingest --connection handbook_notion` would then fetch those pages and reconcile
them into `knowledge/global/*.md`, the same as any other evidence source. To make that
concrete without a live workspace or token, `notion_demo.py` runs the *exact* connector code
(`NotionFetchAdapter` + `NotionExtractionSkill`) against the sample pages here through a local
`NotionPageSource` instead of the Notion API:

```sh
python notion_demo.py
```

It prints one `DocEvidence` record per sample page: title, `usage_hint`, `topic_refs`,
fingerprint. Compare those to `usage_mode`/`tags` in `knowledge/global/*.md`: the five files
there are curated from these five pages and keep their `usage_mode`. An ingest would draft them with
`provenance: inferred` and a `source_fingerprint`, and the curated files add tags and bound fingerprints on top. That's how the evidence gets
used later. `search_knowledge("revenue reporting policy")` finds them, and any query touching
`total_revenue` auto-surfaces the `caveat`-mode pages alongside the answer (see
[Knowledge pages](../../docs/guides/ecommerce.mdx#knowledge-pages) in the full guide).

## What's in here

```
canonic.yaml                              ← project config + Postgres connection + dbt connection
setup.sql                                 ← CREATE TABLE + seed data
dbt/manifest.json                         ← dbt manifest mirroring the star schema. Runs offline
notion_demo.py                            ← offline demo: docs/notion-pages/*.md → real DocEvidence
candidates.yaml                           ← local model candidates for canonic eval baseline
eval/grain_cases.jsonl                    ← labeled grain-inference cases
semantics/warehouse_pg/                   ← orders, orders_rt, order_items, customers, products, channels
contracts/metrics/                        ← revenue, order-count, units-sold
contracts/guardrails/                     ← revenue-excludes-refunds, units-sold-excludes-refunds,
                                             board-reporting-final-only, finality-revenue
contracts/assertions/                     ← 3 seed-derived expected values for canonic assert
raw-sources/                              ← evidence snapshots of the last ingest run
knowledge/global/                         ← 5 definition/caveat/policy pages
docs/notion-pages/                        ← sample Notion page sources for the DocEvidence connector.
                                             See notion_demo.py to turn them into evidence
```
