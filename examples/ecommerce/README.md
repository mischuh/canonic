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
