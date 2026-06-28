# SaaS Analytics — Canon Example

A complete, self-contained **data-warehouse** example for Canon: a SaaS subscription business
modelled Kimball-style as a **Business Vault** (dimensions + facts, including a monthly snapshot
fact) with a condensed **Data Mart** layer on top — all in a single bundled **DuckDB** file.

It is the broadest example in this repo: it exercises **every metric binding kind Canon supports**,
all three guardrail kinds, finality/restrict-source, query-based assertions, and a global knowledge
base — in one place.

| Feature | How it's shown |
|---------|---------------|
| **DuckDB primary connector** | `saas.duckdb` — zero server setup, fully local |
| **DWH layering** | Business Vault (`dim_*`, `fct_*`) → Data Marts (`mart_*`) |
| **Snapshot fact** | `fct_mrr_snapshot` — drives the semi-additive `ending_mrr` |
| **Full metric spectrum** | all 7 binding kinds (see catalogue below) |
| **Guardrails** | `mandatory_filter`, `restrict_source` (+ finality), `required_dimension` |
| **Assertions** | query-based, seed-derived expected values (`canon assert` → 100%) |
| **Knowledge** | `knowledge/global/` — definitions, caveats, policies |
| **MCP serving** | `canon mcp start` — expose everything to any MCP agent |

## Schema

Business Vault (8 dimensions, 10 facts) + 4 data marts:

```
                       dim_geo   dim_industry   dim_campaign   dim_sales_rep
                          \          |              |    \        /   |
                           \         |              |     \      /    |
   dim_plan ─────< fct_mrr_snapshot  └──< dim_customer >──┘  fct_opportunities
       │           fct_subscription_events │                fct_support_tickets
       │           fct_invoices / _rt       │                fct_payments
       │           fct_usage                │                fct_nps_responses
   dim_feature ──< fct_feature_usage ───────┘

   Data Marts:  mart_monthly_mrr · mart_cohort_retention · mart_account_health · mart_rep_quota
```

| Table | Rows | Description |
|-------|------|-------------|
| `dim_customer` | 12 | One row per customer account (conformed dimension) |
| `dim_plan` / `dim_geo` / `dim_industry` | 4 / 5 / 5 | Plan catalogue, geography, industry |
| `dim_sales_rep` / `dim_campaign` / `dim_feature` | 6 / 5 / 8 | Reps, acquisition campaigns, product features |
| `dim_date` | 731 | Conformed daily calendar (2024–2025) |
| `fct_mrr_snapshot` | 144 | **Snapshot** — MRR position per customer per month |
| `fct_subscription_events` | 18 | Lifecycle events (new/expansion/contraction/churn) |
| `fct_invoices` / `fct_invoices_rt` | 131 / 3 | Final billing + provisional intraday estimates |
| `fct_usage` / `fct_feature_usage` | 130 / 842 | Account usage rollup + per-feature usage |
| `fct_support_tickets` | 24 | Support tickets (CSAT, resolution time) |
| `fct_opportunities` | 20 | Sales pipeline (won/lost deals) |
| `fct_nps_responses` / `fct_payments` | 18 / 130 | NPS survey + payment transactions |
| `mart_*` | — | Pre-aggregated monthly MRR, cohort retention, account health, rep quota |

## Quick start

```bash
cd examples/saas-analytics

# (Optional) rebuild the warehouse from setup.sql
bash scripts/build.sh

# Run a few demo queries (the SemanticQuery JSON shape)
canon query -f <(echo '{"metrics":["ending_mrr"],"dimensions":["snapshot_month"]}')
canon query -f <(echo '{"metrics":["arpu"],"dimensions":["snapshot_month"]}')
canon query -f <(echo '{"metrics":["customer_ltv"],"dimensions":["customer_id"]}')

# Run the contract assertions (gates on accuracy)
canon assert

# Start the MCP server for agent access
canon mcp start
```

No LLM is required: this example ships hand-curated semantics and contracts. The DuckDB connection is
read-only.

## Metric catalogue — all 7 binding kinds

Showcase metrics (in `contracts/metrics/`):

| Kind | Metric(s) | Binding highlight |
|------|-----------|-------------------|
| `single` | `gross_revenue`, `expansion_mrr`, `support_tickets`, `pipeline_value`, `settled_payments`, `avg_csat` | source + measure |
| `ratio` | `arpu`, `churn_rate`, `win_rate`, `cac`, `nps` | numerator / denominator (both single) |
| `weighted_avg` | `blended_discount` | `weighted_sum` / `weight` |
| `semi_additive` | `ending_mrr` | `collapse_dimension: snapshot_month`, `collapse_agg: last` |
| `distinct_count` | `active_subscribers`, `active_features` | `distinct_on` + `population_filter` |
| `percentile` | `median_contract_value`, `p90_resolution_time`, `median_deal_size` | `column` + `quantile` |
| `opaque` | `customer_ltv` | `native_grain: [customer_id]` — served at customer grain only |

Ratio and weighted-avg components must themselves be `single`-kind metrics, so a set of small
**helper metrics** (`mrr_total`, `active_accounts`, `churned_customers`, `new_customers`,
`won_opportunities`, `total_opportunities`, `campaign_spend`, `discount_value_sum`,
`total_contract_value`, `nps_net`, `nps_responses`) provide those building blocks.

## Guardrails

`contracts/guardrails/`:

- **`revenue-excludes-refunds`** / **`revenue-excludes-trials`** — `mandatory_filter` (`error`).
  Inject `status != 'refunded'` and `is_trial = false` into every `gross_revenue` query.
- **`board-reporting-final-only`** — `restrict_source` (`error`). In the `board_reporting` context,
  confines `gross_revenue` to the final `fct_invoices` source. Paired with **`finality-revenue`**,
  which declares the final/provisional realizations and coalescing rule.
- **`ending-mrr-requires-month`** — `required_dimension` (`warn`). Forward-looking P1 contract: it is
  recorded and surfaced but not yet enforced by the compiler.

Try it:

```bash
# refunds + trials are silently excluded; provisional rows excluded under board_reporting
canon query -f <(echo '{"metrics":["gross_revenue"],"context":"board_reporting"}')

# opaque grain guard: this errors with UNSUPPORTED_MEASURE
canon query -f <(echo '{"metrics":["customer_ltv"],"dimensions":["segment"]}')
```

## Assertions

`contracts/assertions/` — query-based, with expected values derived from the deterministic seed:

- `gross-revenue-2025-q1` → `17814.00` (paid, non-trial, non-refunded invoices, Q1)
- `active-subscribers-2025-03` → `12`

`canon assert` runs them and reports accuracy (expected: **100%**).

## Knowledge

`knowledge/global/` — Markdown + frontmatter bound to semantic entities via `sl_refs`:

- `mrr-definition` (definition, with a live `{{ sl:... }}` template)
- `semi-additive-mrr-caveat` (caveat) — never sum MRR across months
- `revenue-excludes-refunds-trials-caveat` (caveat)
- `revenue-finality-policy` (policy) — final vs provisional revenue
- `ltv-methodology` (policy) — why `customer_ltv` is opaque
- `vault-vs-mart` (reference) — when to use vault facts vs data marts

## Regenerating the warehouse

```bash
bash scripts/build.sh   # rebuilds saas.duckdb from setup.sql (idempotent)
```

`setup.sql` is plain, deterministic DDL + seed and can also be run directly through any DuckDB client:
`duckdb saas.duckdb < setup.sql`.
