# Canon ecommerce demo

A minimal but end-to-end Canon project: one Postgres connection, two semantic sources,
one canonical metric, and one enforced guardrail. Enough to run `canon mcp start` and
let an agent call the `query` tool and get real rows back.

## What's in here

```
canon.yaml                              ← project config + Postgres connection
setup.sql                               ← CREATE TABLE + seed data (10 orders, 5 customers)
semantics/warehouse_pg/
  orders.yaml                           ← grain, measures (revenue, order_count), dimensions, join
  customers.yaml                        ← country dimension (join target for orders)
contracts/metrics/
  revenue.yaml                          ← canonical binding: revenue → orders.total_revenue
contracts/guardrails/
  revenue-excludes-refunds.yaml         ← mandatory_filter: status != 'refunded'
knowledge/global/
  revenue-definition.md                 ← usage_mode: definition — what total_revenue means + live expr
  revenue-excludes-refunds-caveat.md    ← usage_mode: caveat — why refunds are excluded (auto-surfaces)
  revenue-reporting-policy.md           ← usage_mode: policy — month-end cutoff rules
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

The `analytics` schema is declared once in `canon.yaml` (`schema: analytics`) and applied
as `search_path` on every session — no need to embed it in the connection string.

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
→ [{"metric": "revenue", "source": "orders", "measure": "total_revenue", "aliases": ["net revenue", "rev"]}]
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

### Three `usage_mode` values in this example

| Page | `usage_mode` | Effect |
| --- | --- | --- |
| `revenue-definition` | `definition` | Canonical prose definition; surfaced by search |
| `revenue-excludes-refunds-caveat` | `caveat` | **Auto-surfaced** when any result references `total_revenue`, even if not searched |
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
result = engine.search("revenue", requesting_user="alice")

# The two non-caveat pages match the query
print([h.page for h in result.hits])
# ['revenue-definition', 'revenue-reporting-policy']

# The caveat page rides along automatically because a hit references total_revenue
print([(c.page, c.triggered_by) for c in result.caveats])
# [('revenue-excludes-refunds-caveat', ['warehouse_pg.orders.total_revenue'])]
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
