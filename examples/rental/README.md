# Canonic rental demo

An end-to-end canonic project on a vehicle rental service: one SQLite connection, five dimensions, four fact tables, nine metric contracts (three canonical), and one enforced guardrail (NULL-handling on `total_amount`).

Full walkthrough, metrics table, and the cross-fact fanout caveat: **[`docs/guides/rental.mdx`](../../docs/guides/rental.mdx)**.

## Prerequisites

- Python ≥ 3.13, Canonic installed (`pip install -e ../..` from this directory)
- `sqlite3` to create the database from `setup.sql`
- No LLM key needed for `status`/`ingest --bootstrap`/`query`/`mcp start` — every table has a declared primary key. `CANONIC_LLM_API_KEY` only matters for the optional `canonic eval baseline` step.

## Quickstart

```sh
sqlite3 rental.db < setup.sql   # create the database (one-time)
cd examples/rental              # canonic commands must run from here
canonic status
canonic ingest --bootstrap
canonic query --metrics rental_count --dimensions status
canonic mcp start
canonic eval baseline \         # optional: grain-inference accuracy
  --candidates candidates.yaml \
  --dataset eval/grain_cases.jsonl
```

## What's in here

```
canonic.yaml                    ← SQLite connection, LLM, reconcile settings
setup.sql                       ← DDL + seed data
semantics/rental_db/            ← 5 dimensions + 4 facts (incl. vehicle_inventory)
contracts/metrics/              ← 9 metric contracts (rental-revenue, rental-count,
                                   avg-rental-duration + 6 more)
contracts/guardrails/           ← completed-rentals-only.yaml
knowledge/global/               ← 5 definition/caveat pages
knowledge/user/client_1/        ← 1 personal note (scoped to the client_1 MCP token)
eval/grain_cases.jsonl          ← 8 labeled grain-inference cases (PK omitted)
```
