# Canon: The Open Context Layer for Data Agents

Canon is an open, file-based **context layer** that sits between your data stack and the AI agents that query it. A database connection alone doesn't make an agent a competent analyst — given raw schema access, an agent still has to guess which table is canonical, which join is safe, which rows are test accounts, and what the business actually means by a metric. Plausible SQL becomes wrong SQL fast.

Canon turns your warehouse metadata, BI definitions, modeling code, query history, and team docs into three reviewable surfaces and serves them to agents at runtime via **MCP** and a **CLI**. Database access is always **read-only**; all context is versioned in git and reviewed like code.

> **Status:** early development. The PRD and Phase 0 specs live in [`docs/`](docs/). This README is a first draft.

---

## Why Canon

Adjacent tools each leave a gap:

- **Company-brain / search tools** index docs and chats but have no join graph, no canonical metrics, and no path to safe SQL.
- **Traditional semantic layers** compile correct SQL but are hand-maintained, don't learn from the surrounding warehouse / BI / query history, and split the *why* (business context) from the *what* (definitions).

Canon combines business context with executable definitions in one surface, and keeps it current as the data stack changes.

## The three surfaces

All three are committed to git and reviewed like code:

| Surface | Format | Nature | Answers |
| --- | --- | --- | --- |
| **Semantics** | `semantics/**/*.yaml` | structured, executable, auto-maintained | "How do I query this safely?" |
| **Knowledge** | `knowledge/**/*.md` | free-form, searchable, auto-maintained | "What does this mean to the business?" |
| **Contracts** | `contracts/**/*.yaml` | authoritative, enforceable, human-owned | "Which definition is canonical, and what must the answer obey?" |

**Split rule:** if a fact changes how the SQL runs → semantics. If a human needs it to trust the answer → knowledge. If it governs *which* definition is authoritative or *what an answer must satisfy* → contracts.

Knowledge caveats that describe a correctness risk (e.g. *"`amount` includes refunds unless filtered"*) become **enforced contracts** — mandatory filters, required dimensions, or hard compiler warnings — not just documentation. The knowledge page explains *why*; the contract makes the SQL *obey*.

Every surface carries last-validated-against-source metadata, so serving can warn agents when a definition is stale instead of silently trusting it.

## What Canon does

Canon has two connected sides:

1. **Build & maintain context** — ingest source systems and reconcile new evidence with accepted definitions.
2. **Serve agents at runtime** — search context, return approved metrics, compile them into dialect-correct SQL.

Five core capabilities live in a protocol-neutral core and are exposed through thin adapters (MCP, CLI, headless/CI):

- `search` — find context
- `resolve` — name → definition
- `compile` — semantic query → SQL
- `query` — run read-only SQL
- `propose` — patch + validate context

## Project layout

```text
my-project/
├── canon.yaml                  # project config + connections
├── semantics/
│   └── <connection-id>/*.yaml
├── knowledge/
│   ├── global/*.md             # shared business context
│   └── user/<user-id>/*.md     # user-scoped notes (strictly additive)
├── contracts/
│   ├── metrics/*.yaml          # canonical metric→source bindings
│   ├── guardrails/*.yaml       # enforced filters, finality, access policy
│   └── assertions/*.yaml       # query→expected-result checks
├── raw-sources/<connection-id>/   # ingest artifacts & reports
└── .canon/                     # local runtime state + secrets, git-ignored
```

Commit config, semantics, knowledge, and contracts. Keep local state out of the repo.

## How it works

**Ingestion** — `Source connectors → Context builder → Reconciliation → Validation → outputs`. Connectors read each system in its native shape; the builder turns evidence into proposed updates; reconciliation merges new evidence with accepted context (higher provenance tiers win, frozen facts are never overwritten); validation checks references and semantics. Every committed change is traceable to its evidence.

**Serving** — `Agent (plain language) → Canon (MCP/CLI) → search knowledge + semantics → return approved metrics → compile to SQL → read-only DB executes`. Canon is the only component touching both the context files and the warehouse, and every DB connection is read-only.

### Operating modes

| | **Interactive / agent mode** | **Headless / pipeline mode** |
| --- | --- | --- |
| Driver | Human or agent client (Claude Code, Cursor, Codex) via MCP/CLI | Scheduler or CI runner (cron, GitHub Actions, Airflow) |
| LLM in loop | Yes — search, reconciliation drafting, knowledge authoring | No — deterministic |
| Output | Answers, proposed diffs, trust-scored results | Exit codes, emitted SQL/models, auto-PRs, validation reports |

The compiler is deterministic (YAML → SQL, no LLM in the hot path), so the compile/query path is pipeline-safe. In headless mode Canon can run scheduled ingest, act as a **data-contract CI gate**, or emit SQL/models for a transformation step to materialize.

## Sources

Connectors fall into three capability-based classes (the core asks *what a connector can do*, never *who it is*):

- **Primary / queryable** (databases) — PostgreSQL, Snowflake, BigQuery, SQLite. Schema, columns, keys, row counts, query history. Feeds the semantic layer + compiler.
- **Definition** (dbt, LookML, MetricFlow) — measures, dimensions, models, joins, entities. Feeds the semantic layer.
- **Evidence** (Metabase, Looker, Notion, text) — dashboards, usage, prose. Feeds knowledge + reconciliation.

Every connector normalizes its output into one internal evidence schema, so new vendors never leak into the core. A versioned connector contract and out-of-tree plugin SDK let third parties add sources without forking.

## Design principles

- **Read-only by design** — Canon never writes to source data.
- **Reviewable** — every change is a git diff traceable to source evidence.
- **Local-first** — runs on your machine; secrets and runtime state stay local and git-ignored.
- **Interoperable** — ingests existing dbt / MetricFlow / LookML / BI definitions instead of forcing migration.
- **Offline / air-gapped capable** — local LLM + local embeddings keep all content on-machine.
- **Not an orchestrator** — Canon gates and feeds pipelines but owns neither scheduling nor the write-path. dbt / Airflow / Dagster keep ownership of execution.

## What works today

Phase 0 is under active development. The pieces below are implemented and testable now.

### Schema import from DDL (tier 4)

When catalog access is blocked, import schema from a `CREATE TABLE` statement:

```python
from canon.connectors.acquisition import relations_from_ddl

ddl = """
CREATE TABLE analytics.fct_orders (
    order_id  bigint PRIMARY KEY,
    amount    numeric(12,2),
    metadata  jsonb,
    order_date date
);
"""

schemas = relations_from_ddl(ddl, connection="warehouse_pg")
s = schemas[0]
# s.acquisition_tier == "declarative"
# s.primary_key      == ["order_id"]
# s.columns[1].type  == "decimal"   ← native types normalized automatically
```

### Hand-authored semantic source (tier 6)

Describe a relation in `semantics/<connection-id>/<name>.yaml` and load it into the same evidence shape:

```yaml
# semantics/warehouse_pg/orders.yaml
name: orders
connection: warehouse_pg
table: analytics.fct_orders
grain: [order_id]
columns:
  - { name: order_id, type: int }
  - { name: amount,   type: decimal }
measures:
  - { name: total_revenue, expr: "sum(amount)" }
```

```python
from canon.semantic.loader import load_semantic_source
from canon.connectors.acquisition import relations_from_semantic_sources
from pathlib import Path

source = load_semantic_source(Path("semantics/warehouse_pg/orders.yaml"))
schemas = relations_from_semantic_sources([source])
# schemas[0].acquisition_tier == "hand_authored"
# schemas[0].source_fingerprint == "sha256:…"
```

### Validation probe

Before trusting declarative or hand-authored schema, run a zero-scan probe against the live source. It issues `SELECT <columns> FROM <table> WHERE false`, compares declared vs. observed columns and types, and stamps `last_validated_at` on success:

```python
import asyncio
from canon.connectors.acquisition import probe_schema

result = asyncio.run(probe_schema(connector, schemas[0]))

if result.ok:
    print(result.validated.last_validated_at)   # stamped
    print(result.validated.source_fingerprint)  # sha256 over live schema
else:
    result.raise_for_status()
    # raises SchemaMismatch: "amount: declared decimal, observed string"
```

### PostgreSQL live introspection (tier 1)

With a real Postgres connection, `introspect_schema()` returns normalized `RelationSchema` evidence for every table, view, and materialized view — including primary keys, foreign keys, and row-count estimates:

```python
import asyncio
from canon.connectors.postgres import PostgresConnector

connector = PostgresConnector(connection)
schemas = asyncio.run(connector.introspect_schema())
# schemas[i].acquisition_tier == "live"
# schemas[i].primary_key, .foreign_keys, .row_count_estimate populated
```

### CLI skeleton

```sh
canon --version
canon status          # show project root + config version
canon connection list  # registered connections
canon --help
```

---

## Status & roadmap

Canon ships in phases (see [`docs/PRD-canon-final.md`](docs/PRD-canon-final.md) §9.1):

- **Phase 0 — Walking skeleton.** End-to-end for one database: foundation + install (E1), one primary connector (E2), compiler + minimal contracts (E5 + E15), CLI (E7), MCP serving (E8). An agent asks for a metric → Canon resolves the canonical binding → compiles read-only SQL → returns a result. No LLM in this path.
- **Phase 1 — v1 core.** Auto-built context across pillars and multiple sources: ingestion + reconciliation (E4), knowledge + retrieval (E6), LLM config incl. local/offline (E10), definition + evidence connectors (E3), fuller contracts, accuracy tracking.
- **Phase 2 — Trust & operations.** Cost control (E13), answer trust score (E14), feedback loop (E11), agent edit/review loop (E9), governance: RLS/PII + locked context versions (E12).

## Documentation

- [`docs/PRD-canon-final.md`](docs/PRD-canon-final.md) — full product requirements
- [`docs/SPEC-E1-foundation-config-distribution.md`](docs/SPEC-E1-foundation-config-distribution.md) — project foundation, config, distribution
- [`docs/SPEC-E2-primary-source-connector.md`](docs/SPEC-E2-primary-source-connector.md) — primary source connector
- [`docs/SPEC-E5-E15-semantics-and-contracts.md`](docs/SPEC-E5-E15-semantics-and-contracts.md) — semantic layer, compiler & contract surface
- [`docs/SPEC-E7-E8-serving-surfaces.md`](docs/SPEC-E7-E8-serving-surfaces.md) — CLI & MCP serving surfaces
