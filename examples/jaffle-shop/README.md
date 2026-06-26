# Jaffle Shop — Canon Example

A Canon project backed by the classic [dbt Jaffle Shop](https://github.com/dbt-labs/jaffle-shop)
dataset. It demonstrates Canon's full Phase 1 feature set:

| Feature | How it's shown |
|---------|---------------|
| **DuckDB primary connector** | `jaffle_shop.duckdb` — zero server setup, fully local |
| **dbt manifest knowledge** | `dbt/manifest.json` — MetricFlow semantic models + metrics as modeling-tier evidence |
| **Evidence reconciliation** | dbt definitions outrank live introspection; grain/joins/measures arrive pre-named |
| **Knowledge pages** | `knowledge/global/` — business caveats and definitions (fanout, revenue, segmentation) |
| **Metric contracts** | `contracts/metrics/` — revenue, order_count, units_sold |
| **MCP serving** | `canon mcp start` — expose metrics to any MCP-compatible agent |

## Schema

```
customers ──< orders ──< order_items >── products
                 │
               stores
```

| Table | Rows | Description |
|-------|------|-------------|
| `customers` | 20 | Individual and business accounts |
| `stores` | 5 | Physical Jaffle Shop locations |
| `products` | 10 | Jaffles and beverages |
| `orders` | 25 | One row per order with payment breakdown |
| `order_items` | 37 | One row per line item |

## Quick start

```bash
cd examples/jaffle-shop

# Bootstrap — introspects DuckDB + loads dbt manifest as modeling-tier evidence
canon ingest --bootstrap

# Run a demo query (revenue by store)
canon query '{"metric": "revenue", "group_by": ["store_id"]}'

# Start the MCP server for agent access
canon mcp start
```

No LLM is required for the deterministic bootstrap. Set `CANON_LLM_API_KEY` and
configure `llm:` in `canon.yaml` to enable grain-drafting for low-confidence proposals.

## Artifacts

- **`jaffle_shop.duckdb`** — pre-built database (generated from Jaffle Shop seeds)
- **`dbt/manifest.json`** — compiled manifest (schema v11, dbt 1.7, MetricFlow)

### Regenerating artifacts

Install dbt and run:

```bash
bash scripts/build.sh
```

This clones the upstream `dbt-labs/jaffle-shop`, runs `dbt build` with `dbt-duckdb`,
and copies the resulting database and manifest back into this directory.

## What Canon extracts from the dbt manifest

The `jaffle_dbt` connection parses `manifest.json` as **modeling-tier evidence** — it
ranks higher than live DuckDB introspection during reconciliation. From this manifest,
Canon extracts:

- **5 model nodes** → `RelationSchema` with named columns, types, primary keys, and
  foreign key paths
- **2 semantic models** → ENTITY (grain), FOREIGN JOIN paths, named MEASURE and DIMENSION
  definitions
- **3 metrics** → `revenue`, `order_count`, `units_sold`

The result: after `canon ingest --bootstrap`, semantic sources in `semantics/jaffle_duckdb/`
carry business-meaningful measure names (`revenue`, `order_count`) rather than generic
inferred ones (`total_amount`, `row_count`).
