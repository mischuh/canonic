# Canonic rental demo

An end-to-end Canonic project built on a vehicle rental service: one SQLite connection,
five dimensions, three fact tables, three canonical metrics, and two enforced guardrails.
No external database required — the entire dataset lives in a single `rental.db` file.

## Schema

```
DIMENSIONS
  vehicle_categories   category_id · name · daily_rate
  locations            location_id · code · city · country · airport_code
  employees            employee_id · name · title · location_id
  customers            customer_id · name · email · country · membership_tier
  vehicles             vehicle_id · make · model · year · category_id

FACTS
  rentals              rental_id · customer_id · vehicle_id · pickup/dropoff_location
                       employee_id · dates · planned_days · actual_days
                       rate_per_day · total_amount · status
  payments             payment_id · rental_id · payment_date · amount · method · status
  damages              damage_id · rental_id · vehicle_id · severity · repair_cost
```

**Seed data**: 40 rentals (32 completed, 3 active, 2 confirmed, 2 cancelled, 1 no_show),
32 settled payments, 8 damage claims — across 20 customers, 15 vehicles, 5 locations.

## Quick start

```bash
# 1. Create the database (one-time)
sqlite3 rental.db < setup.sql

# 2. Set your LLM key (adjust canonic.yaml if you use a different provider)
export CANONIC_LLM_API_KEY=<your-key>

# 3. Verify the project is recognised
canonic status

# 4. Bootstrap the semantic layer from the live schema
canonic ingest --bootstrap

# 5. Start the MCP server so agents can query it
canonic mcp start

# 6. (Optional) Run grain-inference accuracy baseline
canonic eval baseline \
  --candidates candidates.yaml \
  --dataset eval/grain_cases.jsonl
```

## Phase 1 loop

| Step | What it proves |
|---|---|
| `canonic ingest --bootstrap` | Bootstraps context from a real SQLite stack |
| `query()` + `search_knowledge()` via MCP | Agents get executable definitions + business meaning |
| `canonic eval baseline` | Grain-inference accuracy is tracked |

## Metrics

| Metric | Source · Measure | Canonical for |
|---|---|---|
| `rental_revenue` | `payments.total_paid` | settled revenue across all payment methods |
| `rental_count` | `rentals.completed_rental_count` | number of completed agreements |
| `avg_rental_duration` | `rentals.avg_rental_days` | mean actual days held per completed rental |

## Guardrails

**`completed-rentals-only`** — `rentals.total_base_revenue` must never be summed without a
`status = 'completed'` filter. Active and cancelled rows have NULL in `total_amount`.
Use `payments.total_paid` for financial reporting.

> **Cross-fact fanout note** (documented in knowledge, not yet a guardrail kind): joining
> `damages` to `payments` in a single query fans out both facts. Always bridge each
> independently through `rentals`.

## Files

```
canonic.yaml                           ← project config — SQLite connection, LLM, reconcile settings
setup.sql                            ← DDL + seed data; creates the rental.db file
semantics/rental_db/
  vehicle_categories.yaml            ← dim: category name, daily rate
  locations.yaml                     ← dim: city, country, airport code
  employees.yaml                     ← dim: title, location
  customers.yaml                     ← dim: country, membership tier
  vehicles.yaml                      ← dim: make, model, year, category
  rentals.yaml                       ← fact: rental_count, total_base_revenue, avg_rental_days
  payments.yaml                      ← fact: payment_count, total_paid, avg_payment_amount
  damages.yaml                       ← fact: damage_count, total_repair_cost
contracts/metrics/
  rental-revenue.yaml                ← canonical binding: rental_revenue → payments.total_paid
  rental-count.yaml                  ← canonical binding: rental_count → rentals.completed_rental_count
  avg-rental-duration.yaml           ← canonical binding: avg_rental_duration → rentals.avg_rental_days
contracts/guardrails/
  completed-rentals-only.yaml        ← inject status='completed' filter on total_base_revenue
knowledge/global/
  rental-revenue-definition.md       ← what rental_revenue counts and excludes
  rental-duration-note.md            ← planned_days vs actual_days, NULLs, early returns
eval/
  grain_cases.jsonl                  ← 8 labeled grain-inference cases (PK omitted)
```



cat > q.json <<'EOF'
{"dimensions": ["status"], "metrics": ["rental_count"]}
EOF