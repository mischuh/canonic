# Canon ecommerce demo

A small but end-to-end Canon project: one Postgres connection, a four-source star schema
(two facts, three dimensions), three canonical metrics, and one enforced guardrail.
Covers the complete **Phase 1 loop**: ingest bootstraps context from a real stack, the
MCP server gives agents both executable definitions and business meaning, and `canon eval`
tracks accuracy.

## Phase 1 loop

```
canon ingest --bootstrap          # 1. bootstrap: introspect Postgres → write semantics/*.yaml
canon mcp start                   # 2. serve: agents call query() + search_knowledge() together
canon eval baseline \             # 3. track: measure grain-inference accuracy on the live schema
  --candidates candidates.yaml \
  --dataset eval/grain_cases.jsonl
```

Each step proves one Phase 1 exit criterion:

| Step | Criterion |
| --- | --- |
| `canon ingest --bootstrap` | Bootstraps context from a real stack |
| `query()` + `search_knowledge()` | Agents get both executable definitions and business meaning |
| `canon eval baseline` | Accuracy is tracked |

## What's in here

```
canon.yaml                              ← project config + Postgres connection
setup.sql                               ← CREATE TABLE + seed data (10 orders, 17 line items,
                                          5 customers, 5 products, 3 channels)
candidates.yaml                         ← local model candidates for canon eval baseline
eval/grain_cases.jsonl                  ← labeled grain-inference cases for the ecommerce schema
semantics/warehouse_pg/
  orders.yaml                           ← fact: revenue/order_count measures, joins to customers + channels
  order_items.yaml                      ← fact: line_revenue/units_sold, joins to orders + products
  customers.yaml                        ← dim: country (join target for orders)
  products.yaml                         ← dim: product_name, category (join target for order_items)
  channels.yaml                         ← dim: channel name (join target for orders)
contracts/metrics/
  revenue.yaml                          ← canonical binding: revenue → orders.total_revenue
  order-count.yaml                      ← canonical binding: order_count → orders.order_count
  units-sold.yaml                       ← canonical binding: units_sold → order_items.units_sold
contracts/guardrails/
  revenue-excludes-refunds.yaml         ← mandatory_filter: status != 'refunded'
knowledge/global/
  revenue-definition.md                 ← usage_mode: definition — what total_revenue means + live expr
  revenue-excludes-refunds-caveat.md    ← usage_mode: caveat — why refunds are excluded (auto-surfaces)
  revenue-reporting-policy.md           ← usage_mode: policy — month-end cutoff rules
  units-sold-definition.md              ← usage_mode: definition — what units_sold means + live expr
  order-items-fanout-caveat.md          ← usage_mode: caveat — line-item fanout trap (auto-surfaces)
```

## Prerequisites

- Python ≥ 3.13, Canon installed (`pip install -e ../..` from this directory)
- A Postgres database you can write to (local Docker, Neon free tier, etc.)

## Setup

**1. Create the tables and seed data:**

```sh
export CANON_PG_PASSWORD=postgres   # password for the postgres user
psql "postgres://postgres:${CANON_PG_PASSWORD}@localhost:5432/postgres" < setup.sql
```

The script is **idempotent** — re-running it drops and recreates all tables in the correct
order, so schema changes (e.g. a new column) are always applied cleanly. The `analytics`
schema is declared once in `canon.yaml` (`schema: analytics`) and applied as `search_path`
on every session — no need to embed it in the connection string.

**2. Verify the project is recognised:**

```sh
cd examples/ecommerce   # ← must run canon commands from here
canon status
# Canon project: ecommerce-demo (version 1)
# Root: /path/to/examples/ecommerce
# Connection: warehouse_pg (postgres)
```

## Start the MCP server

**Stdio — for Claude Code / Cursor (the MCP client owns the process):**

```sh
canon mcp start
```

Add this to your Claude Code MCP config (`~/.claude.json` or the project `.claude.json`):

```json
{
  "mcpServers": {
    "canon": {
      "command": "canon",
      "args": ["mcp", "start"],
      "cwd": "/absolute/path/to/examples/ecommerce"
    }
  }
}
```

**HTTP daemon — background process, multiple clients can connect:**

```sh
canon mcp start --http --port 7474
canon mcp status   # shows: running | PID | version | transport
canon mcp stop     # SIGTERM + removes .canon/mcp.json
```

If the daemon starts but immediately dies (e.g. port already in use, import error), check
`.canon/mcp.log` — stdout and stderr from the daemon process are written there:

```sh
tail -f .canon/mcp.log
```

Connect your MCP client to the running daemon — no `command`/`args`, just a URL:

**Claude Code** (`~/.claude.json` or project `.claude.json`):
```json
{
  "mcpServers": {
    "canon": {
      "transport": "streamable-http",
      "url": "http://127.0.0.1:7474/mcp"
    }
  }
}
```

The server uses FastMCP's **Streamable HTTP** transport (MCP spec 2025-03-26) at `/mcp`.
If your client only supports SSE, use `/sse` instead.

Only `--http` mode writes `.canon/mcp.json`. In stdio mode the MCP client owns the
process — no state file is created and `.canon/` stays absent; that is expected.

## Example tool calls

Once the MCP server is running, an agent can call these tools:

**List all canonical metrics:**
```json
list_metrics()
→ [
    {"metric": "revenue",     "source": "orders",      "measure": "total_revenue", "aliases": ["net revenue", "rev"]},
    {"metric": "order_count", "source": "orders",      "measure": "order_count",   "aliases": ["orders", "number of orders"]},
    {"metric": "units_sold",  "source": "order_items", "measure": "units_sold",    "aliases": ["units", "quantity sold"]}
  ]
```

**Revenue by day (guardrail fires automatically):**
```json
query({"metrics": ["revenue"], "dimensions": ["order_date"]})
→ {
    "result": {
      "columns": [{"name": "order_date", "type": "timestamp"}, {"name": "total_revenue", "type": "decimal"}],
      "rows": [["2025-01-10T00:00:00", 500.0], ["2025-01-12T00:00:00", 350.0], ...]
    },
    "compiled": {"sql": "SELECT … WHERE \"orders\".\"status\" <> 'refunded' …", "dialect": "postgres"},
    "metadata": {
      "resolved": {"metrics": {"revenue": "orders.total_revenue"}},
      "guardrails_fired": [{"id": "revenue-excludes-refunds", "kind": "mandatory_filter"}],
      "freshness": [{"source": "orders", "last_validated_at": null, "stale": false}]
    }
  }
```

**Revenue by country (uses the many_to_one join to customers):**
```json
query({"metrics": ["revenue", "order_count"], "dimensions": ["country"]})
```

**Revenue by sales channel (many_to_one join orders → channels):**
```json
query({"metrics": ["revenue"], "dimensions": ["channel"]})
```

**Units sold by product category (order_items → products):**
```json
query({"metrics": ["units_sold"], "dimensions": ["category"]})
→ {"result": {"columns": [{"name": "category", …}, {"name": "units_sold", …}],
              "rows": [["Accessories", …], ["Displays", …], ["Furniture", …]]}, …}
// units_sold across non-refunded orders totals 33
```

**Compile only — no execution:**
```json
compile_query({"metrics": ["revenue"], "dimensions": ["order_date"]})
→ {"sql": "SELECT …", "dialect": "postgres", "resolved": {…}, "guardrails_fired": […]}
```

**Ambiguous name → structured error, no crash:**
```json
resolve_metric("rev")
→ {"metric": "revenue", "source": "orders", "measure": "total_revenue"}
```

**Business meaning alongside executable SQL — search_knowledge:**
```json
search_knowledge("revenue reporting policy")
→ {
    "hits": [
      {
        "page": "revenue-reporting-policy",
        "summary": "Month-end cutoff rules for revenue reporting.",
        "usage_mode": "policy",
        "matched_on": ["lexical"],
        "sl_refs": ["warehouse_pg.orders.total_revenue"]
      }
    ],
    "caveats": [
      {
        "page": "revenue-excludes-refunds-caveat",
        "summary": "Revenue figures exclude orders with status = 'refunded'.",
        "triggered_by": ["warehouse_pg.orders.total_revenue"]
      }
    ]
  }
```

The `search_knowledge()` tool surfaces knowledge pages by topic. Caveats are
**auto-surfaced** whenever a hit's bound semantic entity (`sl_refs`) matches a caveat
page — so the refund caveat rides along whenever any revenue topic is returned, even
if the query was about reporting policy, not refunds.

A typical agent pattern is `query()` for executable SQL + `search_knowledge()` for
business context — both calls together, one decision:

```json
// Step 1: get rows
query({"metrics": ["revenue"], "dimensions": ["order_date"]})

// Step 2: understand the business rules
search_knowledge("revenue definition")
→ { "hits": [{"page": "revenue-definition", "usage_mode": "definition", …}],
    "caveats": [{"page": "revenue-excludes-refunds-caveat", …}] }
```

## Ingestion — keep semantics current as the schema evolves

`canon ingest` refreshes the semantic files from the live Postgres schema.  The demo project
ships with **hand-authored** (`provenance: human_curated`) files; an ingest run reconciles the
live schema against them and surfaces any drift as a reviewable diff — without overwriting the
curated definitions silently.

**Dry run — see what would change, write nothing:**

```sh
canon ingest --dry-run
# Decisions: add: 0, no_op: 2, …
# (no_op because orders.yaml and customers.yaml already match the live schema)
```

**Bootstrap a connection from scratch** (for a fresh project without hand-authored files):

```sh
canon ingest --bootstrap
# Introspects warehouse_pg → writes semantics/warehouse_pg/*.yaml deterministically
```

**Full ingest — propose diffs for review:**

```sh
canon ingest
# Writes raw-sources/warehouse_pg/evidence.jsonl (committed, reproducible)
# Writes .canon/ingest-events.jsonl             (local audit log, git-ignored)
# Edits no committed semantics in place
```

**JSON output — machine-readable reconciliation report:**

```sh
canon --json ingest --dry-run
# {"diffs": […], "notes": [], "report": {"entries": […], "summary": {"add": 0, "no_op": 2, …}}}
```

**Headless / CI — deterministic pipeline + auto-PR:**

```sh
# Same result on every run with the same schema (identical proposals, identical JSON):
canon ingest --headless --no-pr

# Full CI recipe: open a PR if diffs exist, fail on contradictions:
canon --json ingest --headless --strict
# exit 0  → clean run (PR opened if diffs, or no-op)
# exit 9  → VALIDATION_FAILED — proposed output invalid, no PR
# exit 13 → CONNECTION_ERROR  — Postgres unreachable
# exit 14 → CONTRADICTION     — --strict flagged a drift that conflicts with a curated fact
```

Example GitHub Actions job (add to `.github/workflows/`):

```yaml
- name: Canon ingest
  run: canon --json ingest --headless --strict
  working-directory: examples/ecommerce
  env:
    CI: "true"
    CANON_PG_PASSWORD: ${{ secrets.CANON_PG_PASSWORD }}
```

**Contradiction example** — what happens when schema drift conflicts with a curated fact:

The `orders.yaml` and `customers.yaml` files carry `provenance: human_curated`.  If the live
Postgres schema diverges from those definitions (e.g. a column type changes), ingest flags a
`contradiction` entry in the report but **keeps the curated file untouched**.  With `--strict`
the run exits 14; without it, the contradiction note rides into the PR body for a human to
resolve.

## Accuracy tracking — `canon eval baseline`

`canon eval` measures how accurately a local model infers grain from schema alone (SPEC-E10
§7). The harness runs the production `draft` path over the labeled cases in
`eval/grain_cases.jsonl` and writes a markdown report.

**Run the baseline against your local model:**

```sh
# Point candidates.yaml at your running model server, then:
canon eval baseline \
  --candidates candidates.yaml \
  --dataset eval/grain_cases.jsonl \
  --out docs/baseline-models.md
# gemma-4-e2b-it-4bit: accuracy 80%, structured-output 100%, p50 310 ms ✓ recommended
```

The harness scores each case as correct/incorrect (exact grain match), records structured-output
adherence (did the model honor the JSON schema), and reports p50 latency + median tokens. A
model must clear 90% structured-output adherence to be recommendable — accuracy alone is not
enough if the output is frequently unparseable.

**The five ecommerce cases** in `eval/grain_cases.jsonl` exercise the shape of the live schema:
single surrogate key (`dim_customers`, `dim_channels`, `fct_orders`), descriptive surrogate key
(`dim_products`), and a line-item fact (`fct_order_items`) where `order_item_id` is the grain
rather than the composite `(order_id, product_id)`.

No LLM config is needed to run the rest of the project — `canon eval baseline` is the only
command that makes live model calls.

## CLI usage

The same project works directly from the terminal — no MCP client needed.

**Create a query file:**

```sh
cat > q.json <<'EOF'
{"metrics": ["revenue"], "dimensions": ["order_date"]}
EOF
```

**Human output (Rich table):**

```sh
canon query -f q.json
# ┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
# ┃ order_date          ┃ total_revenue ┃
# ┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
# │ 2025-01-10T00:00:00 │ 500.00        │
# │ …                   │ …             │
```

**Machine output (`--json`) — byte-identical to the MCP `query` tool:**

```sh
canon --json query -f q.json
```

```json
{
  "result":   { "columns": […], "rows": […], "truncated": false },
  "compiled": { "sql": "SELECT … WHERE \"orders\".\"status\" <> 'refunded' …", "dialect": "postgres" },
  "metadata": {
    "resolved":         {"metrics": {"revenue": "orders.total_revenue"}},
    "guardrails_fired": [{"id": "revenue-excludes-refunds", "kind": "mandatory_filter"}],
    "freshness":        [{"source": "orders", "last_validated_at": "…", "stale": false}]
  }
}
```

**Revenue by country (join to customers fires automatically):**

```sh
cat > q.json <<'EOF'
{"metrics": ["revenue", "order_count"], "dimensions": ["country"]}
EOF
canon --json query -f q.json
```

**Raw read-only SQL:**

```sh
canon sql "SELECT status, sum(amount) FROM analytics.fct_orders GROUP BY status"
canon --json sql "SELECT count(*) FROM analytics.fct_orders"

# Non-SELECT is rejected before touching the database:
canon sql "DROP TABLE analytics.fct_orders"
# error: read_only_violation: …
# echo $? → 11
```

**Structured errors on unknown or ambiguous metrics:**

```sh
# exit 2 — metric name matches no active binding
cat > q.json <<'EOF'
{"metrics": ["mrr"]}
EOF
canon --json query -f q.json   # stderr: {"code": "unresolved", "message": "…"}
echo $?                        # 2
```

**CLI vs. MCP — same result:** `canon --json query` and the MCP `query` tool both call
the same `CanonService` and serialize via the same Pydantic model. The walking-skeleton
E2E test (`tests/e2e/test_walking_skeleton.py::test_parity`) asserts byte-identical
payloads against live Postgres on every CI run.

## Event log & observability (`canon report`)

Every query the MCP server or CLI serves appends a `served_answer` event to
`.canon/events.jsonl` — a local, append-only NDJSON file that is git-ignored. Every
`canon ingest` run appends `reconcile_decision` events to the same file. Both kinds share
one unified log; nothing leaves the machine.

**Human report — counts, latency, bytes scanned, error distribution:**

```sh
canon report
# canon report  (telemetry: off)
#
# answers:        42  (2026-06-01T08:00:00Z → 2026-06-19T16:45:12Z)
# latency:        p50 310ms  p95 1240ms  min 85ms  max 2110ms  avg 420ms
# bytes scanned:  total 1,234,567  min 1,024  max 512,000  avg 29,395
# stale answers:  0
# guardrail hits: 38
# ┌──────────────────┬───────┐
# │ code             │ count │
# ├──────────────────┼───────┤
# │ ok               │   40  │
# │ unresolved       │    2  │
# └──────────────────┴───────┘
```

**Restrict to the last N events:**

```sh
canon report --last 100
```

**Machine-readable — for dashboards or CI:**

```sh
canon --json report --last 50
# {"count": 42, "error_distribution": {"ok": 40, "unresolved": 2},
#  "latency": {"p50_ms": 310, "p95_ms": 1240, …},
#  "bytes_scanned": {"total": 1234567, …},
#  "telemetry_enabled": false, …}
```

**What is logged — and what is not:**

| Logged | Not logged |
| --- | --- |
| `query_hash` (SHA-256 of request) | SQL text |
| `compiled_sql_hash` (SHA-256 of compiled SQL) | Result rows |
| `latency_ms`, `bytes_scanned` | Guardrail filter literals |
| `guardrails_fired` (IDs only) | LLM prompts or completions |
| `error` (code string or null) | Any user-supplied query text |

The schema is frozen at contract version `1.1` (SPEC-E16 §6); reserved fields
(`trust_score`, `cache_hit`, `over_limit_blocked`) are present and null until Phase 2.

## Privacy & air-gapped mode

`telemetry.enabled: false` is the default — the local event log is pure local I/O and
nothing is sent off-machine. To enforce this at the config level and prevent it from ever
being enabled accidentally, set `runtime.air_gapped: true`:

```yaml
# canon.yaml
runtime:
  air_gapped: true   # blocks telemetry.enabled: true at load time (exit 18)
```

With `air_gapped: true`, Canon also validates at load time that:

- The LLM `base_url` resolves only to a loopback or explicitly allowlisted address.
- Secret refs (`credentials_ref`, `api_key_ref`) use only local schemes — `env:`, `file:`,
  or `keyring:`. Remote secret services (e.g. `vault:`) are rejected.

The daemon never starts mis-configured — any violation is a hard exit 18 before the first
query is served. `canon status` is the fastest way to confirm a config passes:

```sh
canon status
# Canon project: ecommerce-demo (version 1)   ← load succeeded, all constraints satisfied
```

To add an on-prem inference host outside loopback:

```yaml
runtime:
  air_gapped: true
  allow_cidrs:
    - 10.0.0.0/8   # private inference server at 10.x.x.x
```

## What the guardrail does

The `revenue-excludes-refunds` guardrail is a `mandatory_filter`. Every time a query
touches `orders.total_revenue` the compiler automatically AND-s `status != 'refunded'`
into the WHERE clause and records it in `guardrails_fired`. The seed data has two
refunded orders (IDs 2 and 8, total 260.00) — they never appear in revenue results.

Expected revenue after guardrail: **3790.50** (7 completed + 1 pending order).

## Knowledge pages (E6)

The `knowledge/global/` directory adds searchable context on top of the semantic layer —
the "why" that makes an agent's answers trustworthy, not just technically correct.

### Page format

Each page is Markdown with YAML frontmatter. `scope` and `id` are derived from the path
(`knowledge/global/revenue-definition.md` → global scope, id `revenue-definition`);
everything else is in frontmatter:

```yaml
# knowledge/global/revenue-definition.md
---
summary: "What total_revenue means and how it is calculated."
tags: [revenue, definitions, metrics]
sl_refs:
  - warehouse_pg.orders.total_revenue   # ties this page to the live semantic entity
usage_mode: definition                   # reference | caveat | policy | definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-17T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.orders.total_revenue": "sha256:…"  # drift detection anchor
---

The live SQL — rendered at read time, never a copy:
> `{{ sl:warehouse_pg.orders.total_revenue.expr }}`
```

`{{ sl:<entity>.expr }}` directives are resolved against the live semantic layer at read
time by `DefinitionRenderer`, so the rendered definition can never fall out of sync with
the semantic source.

### `usage_mode` values in this example

| Page | `usage_mode` | Effect |
| --- | --- | --- |
| `revenue-definition` | `definition` | Canonical prose definition; surfaced by search |
| `units-sold-definition` | `definition` | Canonical prose definition for `units_sold`; surfaced by search |
| `revenue-excludes-refunds-caveat` | `caveat` | **Auto-surfaced** when any result references `total_revenue` |
| `order-items-fanout-caveat` | `caveat` | **Auto-surfaced** when any result references `units_sold` or `line_revenue` |
| `revenue-reporting-policy` | `policy` | Business rule page; ranked like `reference` but tagged as policy |

### Search and caveat surfacing (Python API)

```python
from pathlib import Path
from canon.knowledge import (
    KnowledgeSearch, EntityIndex, load_knowledge_page,
)
from canon.semantic.loader import list_semantic_sources

root = Path(".")  # from examples/ecommerce/

# Build the entity index from the live semantic sources
sources = list_semantic_sources(root)
entity_index = EntityIndex.from_sources(sources)

# Load the knowledge pages
pages = [load_knowledge_page(p) for p in (root / "knowledge" / "global").glob("*.md")]

engine = KnowledgeSearch(pages)

# Search for a revenue topic — only the policy page matches; the caveat rides along
# automatically because the hit references warehouse_pg.orders.total_revenue
result = engine.search("month-end cutoff", requesting_user="alice")
print([h.page for h in result.hits])
# ['revenue-reporting-policy']
print([(c.page, c.triggered_by) for c in result.caveats])
# [('revenue-excludes-refunds-caveat', ['warehouse_pg.orders.total_revenue'])]

# Search for a units/product topic — definition page matches; the fanout caveat rides along
# because the hit references warehouse_pg.order_items.units_sold
result2 = engine.search("product category", requesting_user="alice")
print([h.page for h in result2.hits])
# ['units-sold-definition']
print([(c.page, c.triggered_by) for c in result2.caveats])
# [('order-items-fanout-caveat', ['warehouse_pg.order_items', 'warehouse_pg.order_items.units_sold'])]
```

### Live rendering

```python
from canon.knowledge import DefinitionRenderer
from canon.knowledge.loader import load_knowledge_page

page = load_knowledge_page(root / "knowledge/global/revenue-definition.md")
renderer = DefinitionRenderer(entity_index)

print(renderer.render(page))
# … The live SQL — rendered at read time, never a copy:
# > `sum(amount)`           ← the actual expr from orders.yaml
```

If `orders.yaml` were updated to `expr: "sum(amount * fx_rate)"`, the next render would
reflect `sum(amount * fx_rate)` automatically — no page edit needed.

### Drift detection

`meta.bound_fingerprints` records the measure's fingerprint when the page was authored.
If the `expr` in `orders.yaml` changes, `DriftDetector` flags the page for prose review:

```python
from canon.knowledge import DriftDetector

detector = DriftDetector()

# Fingerprints match (orders.yaml is unchanged) → no review needed
print(detector.flagged_for_review(page, entity_index))
# []

# After changing orders.yaml to a different expr, the fingerprint diverges:
# → ['warehouse_pg.orders.total_revenue']
# The flag is a review signal, not a silent edit — the prose "why" may now be wrong.
```
