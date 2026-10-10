# Canonic coffeehouse demo

A small coffee shop in one bundled DuckDB file, and the same questions answered twice: once by a dbt project with MetricFlow, once by canonic. Every dbt definition reads as reasonable and runs without error, yet three of them return a wrong number for a month or a quarter. canonic answers each one correctly, and its guardrails, knowledge pages and assertions keep it that way.

Full walkthrough, every number checked by hand: **[`docs/guides/coffeehouse.mdx`](../../docs/guides/coffeehouse.mdx)**.

## Prerequisites

- Python ≥ 3.13, canonic installed (`pip install -e ../..` from this directory)
- For the dbt side only: `pip install "dbt-metricflow[dbt-duckdb]"` (a separate virtualenv is fine)

## Quickstart

```sh
cd examples/coffeehouse        # canonic commands must run from here
canonic query --metrics average_order_value --dimensions order_month
canonic query --metrics active_customers
canonic assert                 # 4/4 assertions pass
bash scripts/compare.sh        # dbt MetricFlow and canonic side by side
```

## The numbers

| Q1 2026 | dbt MetricFlow | canonic | Why dbt differs |
| --- | --- | --- | --- |
| Average order value | 102.50 | **52.00** | `agg: average` over a line-grain mart averages lines, not orders |
| Active customers | 15 | **5** | a daily distinct count summed into a quarter |
| Revenue per customer | 52.00 | **156.00** | inherits the wrong denominator |
| Units sold | 84 | **69** | a metric added later without the sales filter |
| Distinct customers | 5 | 5 | a distinct count on the line table, shown for fairness |

## What's in here

```
canonic.yaml                  ← project config + DuckDB connection
coffeehouse.duckdb            ← bundled warehouse (4 tables, Q1 2026), rebuilt by scripts/build.sh
setup.sql                     ← DDL + seed data
semantics/coffeehouse_duckdb/ ← customers, products, orders, order_items
contracts/metrics/            ← revenue, units_sold, order_count, average_order_value,
                                 active_customers, revenue_per_customer
contracts/guardrails/         ← 4 mandatory filters: completed orders only, no test customers
contracts/assertions/         ← 4 hand-checked expected values
knowledge/global/             ← what counts as a sale, AOV and active-customer definitions,
                                 2 caveats
dbt/                          ← dbt project with MetricFlow semantic models and metrics
scripts/compare.sh            ← asks dbt and canonic the same questions
```
