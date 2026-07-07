# Canonic SaaS Analytics demo

The broadest example in the set: a SaaS subscription business modeled Kimball-style as a Business Vault + Data Mart, all in a single bundled DuckDB file. Exercises every metric binding kind canonic supports, all three guardrail kinds, finality/restrict-source, and query-based assertions. Ships fully hand-curated — there's no bootstrap step.

Full walkthrough, the all-7-binding-kinds metric catalogue, and guardrail breakdown: **[`docs/guides/saas-analytics.mdx`](../../docs/guides/saas-analytics.mdx)**.

## Prerequisites

- Python ≥ 3.13, Canonic installed (`pip install -e ../..` from this directory)
- Nothing else: `saas.duckdb` is a local file, no server, no credentials, no LLM.

## Quickstart

```sh
cd examples/saas-analytics
bash scripts/build.sh   # optional: rebuild the warehouse from setup.sql
canonic status
canonic query --metrics ending_mrr --dimensions snapshot_month
canonic assert          # runs the contract assertions, gates on accuracy
canonic mcp start
```

There's no `canonic ingest --bootstrap` step: every semantic source, metric, and guardrail already ships `provenance: human_curated`.

## What's in here

```
canonic.yaml                 ← project config + DuckDB connection
saas.duckdb                  ← bundled pre-built warehouse (8 dims, 10 facts, 4 marts)
setup.sql                    ← DDL + seed data (idempotent, rebuild with scripts/build.sh)
semantics/saas_duckdb/       ← hand-curated dimensions + facts
contracts/metrics/           ← 30 metric contracts (12 showcase + helper metrics for ratios)
contracts/guardrails/        ← 4 guardrails + 1 finality rule (all 3 guardrail kinds)
contracts/assertions/        ← 2 query-based assertions, seed-derived expected values
knowledge/global/            ← 6 definition/caveat/policy/reference pages
```
