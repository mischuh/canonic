# Canon: The Open Context Layer for Data Agents

Canon is an open, file-based **context layer** that sits between your data stack and the AI agents that query it. A database connection alone doesn't make an agent a competent analyst — given raw schema access, an agent still has to guess which table is canonical, which join is safe, which rows are test accounts, and what the business actually means by a metric. Plausible SQL becomes wrong SQL fast.

Canon turns your warehouse metadata, BI definitions, modeling code, query history, and team docs into three reviewable surfaces and serves them to agents at runtime via **MCP** and a **CLI**. Database access is always **read-only**; all context is versioned in git and reviewed like code.

> **Status:** early development — Phase 0 walking skeleton complete and serving contract frozen. The compiler, MCP serving surface (E8), CLI capability commands (`canon query`, `canon sql`), and the project setup wizard (`canon setup`) are all live. The serving contract is now versioned (`contract_schema: v1`) and locked behind a conformance gate: JSON schema golden files catch any silent field change in CI. `canon status` and the MCP `contract_info` tool both advertise the version; every `QueryResult.metadata` carries it for provenance. An agent or CI pipeline can call `query` on either surface, receive `QueryResult` with resolved SQL, guardrails fired, freshness, and warnings, and get a structured error on ambiguity — no LLM in the path. CLI `--json` and MCP `query` are verified byte-identical. The PRD and Phase 0 specs live in [`docs/`](docs/).

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

### Contract surface — metric bindings and guardrails

Declare which metric definition is canonical and what rules every query must obey.
Files live in `contracts/metrics/` and `contracts/guardrails/` and are validated like code.

**Define a canonical metric binding** (`contracts/metrics/revenue.yaml`):

```yaml
metric: revenue
canonical:
  source: orders
  measure: total_revenue
provenance: human_curated
aliases: ["net revenue", "rev"]
status: active
```

**Define an enforced guardrail** (`contracts/guardrails/revenue-excludes-refunds.yaml`):

```yaml
id: revenue-excludes-refunds
applies_to:
  source: orders
  measure: total_revenue
kind: mandatory_filter
filter: "status != 'refunded'"
severity: error
rationale: "Refunds are reversals, not revenue."
```

**Load and validate in Python:**

```python
from pathlib import Path
from canon.contracts import load_metric_bindings, load_guardrails, validate_contracts

root = Path(".")

bindings = load_metric_bindings(root)
# bindings[0].metric          == "revenue"
# bindings[0].canonical.source == "orders"
# bindings[0].aliases          == ["net revenue", "rev"]

guardrails = load_guardrails(root)
# guardrails[0].id     == "revenue-excludes-refunds"
# guardrails[0].filter == "status != 'refunded'"

# Cross-surface check: canonical.source/measure must exist in semantics/
validate_contracts(root)
# raises ContractError if applies_to.source or canonical.measure can't be resolved
```

Duplicate active bindings for the same metric name or alias raise a `ContractError` that
names both conflicting files — no silent shadowing.

**Scaffold the directory layout** for a new project:

```python
from canon.contracts import contracts_dir_scaffold
contracts_dir_scaffold(Path("."))
# creates contracts/{metrics,guardrails,assertions}/ if absent
```

### Contract resolver — the canonicality authority

`ContractResolver` is the single integration point between the contract surface and the compiler.
It resolves metric names and returns applicable guardrails deterministically — the compiler calls it
and trusts the result; no canonicality logic lives outside the resolver.

```python
from pathlib import Path
from canon.contracts import ContractResolver, Binding, Ambiguous, Unresolved

resolver = ContractResolver.from_project(Path("."))

# Resolve a metric name or alias
match resolver.resolve_metric("rev"):
    case Binding(metric=metric, source=source, measure=measure):
        print(f"{metric} → {source}.{measure}")
        # revenue → orders.total_revenue
    case Ambiguous(name=name, candidates=candidates):
        print(f"{name} is ambiguous: {[c.metric for c in candidates]}")
    case Unresolved(name=name):
        print(f"{name} has no active binding")

# Guardrails that apply to a (source, measure) pair — stable-sorted by id
guardrails = resolver.guardrails_for("orders", "total_revenue")
# guardrails[0].id     == "revenue-excludes-refunds"
# guardrails[0].filter == "status != 'refunded'"
# guardrails[0].kind   == "mandatory_filter"
```

Calling `resolve_metric` or `guardrails_for` twice with identical arguments returns identical results —
required for deterministic SQL compilation and CI assertions.
Metrics that target an unknown source raise no exception; the result is `Unresolved` and the compiler decides how to surface it.

### Compiler — semantic query → SQL

`compile()` is the deterministic core of the Phase 0 walking skeleton. Given a
`SemanticQuery` (a plain dict of names, never physical tables), a `ContractResolver`, and
the loaded semantic sources, it produces dialect-correct, read-only Postgres SQL plus
full result metadata — no LLM in the path.

The pipeline runs these stages in order:
1. Resolve each metric name → canonical `(source, measure)` via `ContractResolver`
2. Bind every dimension and filter string to its owning source
3. Plan the minimal join path via declared `joins` (ambiguous path → error, never guessed)
4. Detect fanout — additive measures across a one→many join are deduplicated to source grain before aggregating; non-additive measures return `UNSUPPORTED_MEASURE`
5. Enforce guardrails — `mandatory_filter` guardrails are AND-ed into WHERE and listed in `guardrails_fired`
6. Emit SQL through the Postgres dialect adapter — identifiers quoted, `LIMIT` injected, read-only guarantee enforced: non-SELECT statements, locking SELECTs (`FOR UPDATE` / `FOR SHARE`), `SELECT … INTO`, and data-modifying CTEs all raise `ReadOnlyViolation` before any string is produced
7. Attach result metadata — resolved bindings, fired guardrails, per-source freshness

```python
from pathlib import Path
from canon.compiler import SemanticQuery, compile
from canon.contracts import ContractResolver
from canon.semantic.loader import list_semantic_sources

resolver = ContractResolver.from_project(Path("."))
sources  = list_semantic_sources(Path("."))

result = compile(
    SemanticQuery(metrics=["revenue"], dimensions=["order_date"]),
    resolver,
    sources,
)

print(result.sql)
# SELECT DATE_TRUNC('day', "orders"."created_at") AS "order_date",
#        SUM("orders"."amount") AS "total_revenue"
# FROM "analytics"."fct_orders" AS "orders"
# WHERE "orders"."status" <> 'refunded'
# GROUP BY DATE_TRUNC('day', "orders"."created_at")

print(result.resolved)
# {'revenue': 'orders.total_revenue'}

print(result.guardrails_fired)
# [FiredGuardrail(id='revenue-excludes-refunds', kind='mandatory_filter')]

print(result.freshness)
# [SourceFreshness(source='orders', last_validated_at='…', stale=False)]
```

The same query compiled twice produces byte-identical SQL — required for deterministic CI
assertions and result caching (SPEC-E5 §8). Structured errors carry candidates so upstream
callers can act programmatically:

```python
from canon import exc

try:
    compile(SemanticQuery(metrics=["mrr"]), resolver, sources)
except exc.Unresolved as e:
    # e.code == ErrorCode.UNRESOLVED, exit code 2
    print(e)  # metric 'mrr' matches no active binding

try:
    compile(SemanticQuery(metrics=["revenue"], dimensions=["unknown_dim"]), resolver, sources)
except exc.Unreachable as e:
    # e.code == ErrorCode.UNREACHABLE, exit code 4
    print(e)  # dimension 'unknown_dim' is not declared on any source
```

All errors map to structured `ErrorCode` values with canonical headless exit codes
(`UNRESOLVED` → 2, `AMBIGUOUS` → 3, `UNREACHABLE` → 4, `AMBIGUOUS_JOIN_PATH` → 5,
`UNSUPPORTED_MEASURE` → 6, `GUARDRAIL_BLOCK` → 8) — enabling the CI-gate role without
parsing free text.

### Project setup wizard

`canon setup` bootstraps a new project interactively. It writes `canon.yaml`, creates the four context directories, and adds a `.gitignore` covering `.canon/`. Interrupted runs resume from the last completed step — the checkpoint is saved to `.canon/setup-state.json` after each step.

```sh
cd my-new-project
canon setup
```

The wizard walks through five steps:

1. **Project name** — defaults to the current directory name.
2. **First connection** — prompts for Postgres host, port, user, database, and the name of the environment variable holding the password (e.g. `CANON_PG_PASSWORD`). The connection is tested before it is recorded; a failing test re-prompts rather than writing a broken entry.
3. **LLM** — provider, base URL (default `http://localhost:11434/v1` for Ollama), model, and an optional API key env var.
4. **Schema preview** — optionally introspects the live database and reports the number of relations found (no files written).
5. **Write** — emits `canon.yaml`, scaffolds the context directories, and validates the written config by re-loading it.

Secrets are written as env-var references (`credentials_ref: env:CANON_PG_PASSWORD`), never as literal values.

Running `canon setup` inside an existing project enters a menu instead of overwriting committed files:

```sh
# In an existing canon project:
canon setup
# → shows project status and offers:
#   [1] status  [2] add connection  [3] exit
```

After setup completes:

```sh
# Your project tree:
# canon.yaml
# semantics/
# knowledge/
# contracts/
#   metrics/  guardrails/  assertions/
# raw-sources/
# .canon/           ← git-ignored
# .gitignore        ← covers .canon/
```

### Ingestion pipeline — `canon ingest`

`canon ingest` is the four-stage engine that turns live connector evidence into reviewable
context diffs without ever writing to a committed file silently.  It runs:

```
connector introspection
        │
        ▼
1. Context builder      evidence → Proposal[]          (deterministic)
        │
        ▼
2. Reconciliation       Proposal[] × accepted files → ReconciliationReport
        │
        ▼
3. Validation           proposed output state → pass | VALIDATION_FAILED
        │
        ▼
4. Diff emission        reviewable diffs + report      (→ auto-PR in headless mode)
```

Every proposed change is **anchored to evidence** and traceable through the audit trail.
Higher provenance tiers always win — ingest can never silently overwrite a human-curated
or board-approved fact with an inferred one. Contradictions are surfaced as review notes,
never silent overwrites.

#### Bootstrap a new project

On a fresh project, `--bootstrap` runs tier-1 live introspection against the first configured
connection and **writes** the initial `semantics/` files directly — enough to make the agent
useful on day one:

```sh
canon ingest --bootstrap
# Introspects warehouse_pg, writes semantics/warehouse_pg/*.yaml,
# prints the reconciliation report (add: N, no_op: 0, …)
```

Scope to one connection when you have several:

```sh
canon ingest --bootstrap --connection warehouse_pg
```

#### Ongoing ingest — propose-only by default

A regular `canon ingest` refreshes evidence from all connections and emits reviewable diffs.
No committed file is touched — every change becomes a diff for a human to review and merge:

```sh
canon ingest
# Prints a reconciliation summary and the diff set.
# Writes the audit trail under raw-sources/ and .canon/ but edits no semantics in place.
```

**Dry run** — compute and print diffs, write absolutely nothing:

```sh
canon ingest --dry-run

# Machine-readable (includes the full ReconciliationReport + EmissionResult):
canon --json ingest --dry-run
```

#### Re-runs are idempotent

If no upstream schema has changed, `canon ingest` proposes **zero diffs** and only refreshes
`last_validated_at` on unchanged files.  A changed `source_fingerprint` triggers exactly the
affected proposals — nothing else.

#### Reconciliation decisions and provenance

When new evidence conflicts with an existing accepted file the reconciliation engine applies
provenance rules:

| Situation | Decision |
| --- | --- |
| No existing file | `add` — propose the new file |
| Fingerprint matches | `no_op` — refresh `last_validated_at` only |
| Existing tier **higher** than proposal | `contradiction` — flag both sides, keep existing |
| Existing file is **frozen** | `contradiction` — frozen facts are never overwritten |
| Existing tier ≤ proposal, confidence ≥ threshold | `edit` — propose the change |
| Source disappeared | `prune` — propose removing the stale file |

A contradiction is never a hard error by default — it rides into the review surface (or the
auto-PR) for a human to resolve.

#### Strict mode — gate CI on contradictions

Pass `--strict` (or set `reconcile.strict_contradictions: true` in `canon.yaml`) to fail the
run whenever any contradiction is flagged.  The exit is structured, not a bare non-zero:

```sh
canon --json ingest --strict
# If contradictions exist:
#   stderr: {"code": "contradiction", "message": "2 contradiction(s) flagged; …"}
#   exit 14
```

#### Headless / CI mode — deterministic pipeline + auto-PR

`--headless` (or the environment variable `CI=true`, which Canon auto-detects) enables
the safe, repeatable scheduled-ingest role:

- Pins the **deterministic builder** — no LLM on the critical path, so identical evidence
  yields byte-identical proposals across runs.
- After diff emission, opens an **auto-PR** via `git` + `gh` carrying the diffs and
  contradiction notes.
- Returns canonical **exit codes** for every error so the CI runner can gate or route.

```sh
# Headless ingest — auto-PR opened if diffs exist:
canon ingest --headless

# Suppress the PR (headless determinism + exit codes, no git side-effect):
canon ingest --headless --no-pr

# Force a PR even in interactive mode (e.g. for a one-off review request):
canon ingest --open-pr

# Full CI recipe:
canon --json ingest --headless --strict
# exit 0  → clean run, PR opened (or nothing to propose)
# exit 9  → VALIDATION_FAILED — proposed output invalid, no PR opened
# exit 13 → CONNECTION_ERROR  — source unreachable, no PR opened
# exit 14 → CONTRADICTION     — strict mode flagged contradiction(s)
```

**Example GitHub Actions job:**

```yaml
- name: Canon ingest
  run: canon --json ingest --headless --strict
  env:
    CI: "true"
    CANON_PG_PASSWORD: ${{ secrets.CANON_PG_PASSWORD }}
  # exit 0 → clean; 9/13/14 → fail with structured error on stderr
```

The auto-PR carries:
- **PR body** — the full `ReconciliationReport`: decision counts, each diff with its evidence
  anchors and provenance, and the contradiction block.
- **Review comment** — a standalone contradictions summary posted separately so it is easy
  to dismiss once resolved.

The branch name is derived from a hash of the emission's JSON, so a re-run with identical
proposals targets the same branch — no churn, no duplicate PRs.

#### Python API

```python
import asyncio
from pathlib import Path
from canon.config import ReconcileConfig, scaffold_project
from canon.connectors.postgres import PostgresConnector
from canon.ingestion.pipeline import IngestionPipeline
from canon.ingestion.source import evidence_from_introspection

root = Path(".")
scaffold_project(root)

connector = PostgresConnector(connection)
connectors = {"warehouse_pg": connector}
pipeline = IngestionPipeline(root, connectors, ReconcileConfig())

async def run() -> None:
    evidence = await evidence_from_introspection(connector, "warehouse_pg")
    result = await pipeline.run(evidence)          # propose-only
    print(result.emission.render_markdown())       # human-readable summary
    print(result.emission.to_json())               # CI/machine-readable

    # Bootstrap path — writes semantics directly:
    result = await pipeline.bootstrap("warehouse_pg")

asyncio.run(run())
```

**Headless mode — pins the deterministic builder:**

```python
pipeline = IngestionPipeline(root, connectors, ReconcileConfig(), headless=True)
# NullLLMDrafter is used unconditionally — identical evidence → byte-identical output.
```

**Auto-PR — injectable seam for tests:**

```python
from canon.ingestion.autopr import AutoPRPublisher, SubprocessPublisher

publisher = AutoPRPublisher(root, SubprocessPublisher(root))
pr_ref = asyncio.run(publisher.publish(result))
# Calls: git checkout -b canon/ingest-<hash>
#        git add <diff targets>
#        git commit -m "chore(canon): ingest reconciliation — N diffs"
#        gh pr create --title … --body <ReconciliationReport markdown>
#        gh pr comment <pr_ref> <contradiction notes>   (if any)
print(pr_ref)  # https://github.com/…/pull/42
```

#### CLI — query and serving

All capability commands share the `--json` flag for structured, machine-readable output.
Exit codes follow the canonical error registry (see error table below) — the same codes
the MCP surface returns in structured error dicts, so CLI and agent paths are identical.

**Semantic queries:**

```sh
# Compile + execute a semantic query read-only; -f points at a JSON file:
cat > q.json <<'EOF'
{"metrics": ["revenue"], "dimensions": ["order_date"]}
EOF

canon query -f q.json          # human output (Rich table)
canon --json query -f q.json   # machine output (QueryResult JSON)

# Raw read-only SQL escape hatch:
canon sql "SELECT count(*) FROM analytics.fct_orders"
canon --json sql "SELECT count(*) FROM analytics.fct_orders"
```

Both `canon --json query` and the MCP `query` tool return **byte-identical core payloads**
(SPEC §2.1 adapter rule) — the walking-skeleton parity test asserts this end-to-end against
a live Postgres.

Non-SELECT statements are rejected before reaching the database:
```sh
canon sql "DROP TABLE orders"
# error: read_only_violation: …
# exit code 11
```

**MCP daemon control:**

```sh
canon --version
canon status           # show project root, config version, contract_schema
canon connection list  # registered connections
canon --help

# Start the MCP server in stdio mode (foreground; the MCP client owns the process):
canon mcp start

# Or as a background HTTP daemon on localhost:7474:
canon mcp start --http --port 7474

# Lifecycle:
canon mcp status       # running / PID / version / transport
canon mcp stop         # SIGTERM + remove .canon/mcp.json
```

Version compatibility is checked on start: if a daemon is already running with a different
Canon version, the command exits with a clear message instead of starting a second server.

**Headless exit codes:**

| Code | Meaning | Exit |
| --- | --- | --- |
| `UNRESOLVED` | metric name matches no active binding | 2 |
| `AMBIGUOUS` | name matches more than one active binding | 3 |
| `UNREACHABLE` | dimension/filter has no join path | 4 |
| `GUARDRAIL_BLOCK` | severity:error guardrail blocked the query | 8 |
| `VALIDATION_FAILED` | proposed ingest output invalid — no PR opened | 9 |
| `READ_ONLY_VIOLATION` | non-SELECT statement rejected | 11 |
| `CONNECTION_ERROR` | source unreachable during ingest | 13 |
| `CONTRADICTION` | `--strict` ingest flagged one or more contradictions | 14 |

All errors produce a structured `{code, message}` payload on stderr (or via `--json`) so CI
can branch on the code without parsing free text.

### Knowledge pages & retrieval (E6)

The knowledge layer is the "trust" half of Canon: searchable, auto-maintained Markdown pages that
carry business meaning — the *why* that makes an answer trustworthy, not just technically correct.
Every page is committed to git, validated at write time, and kept live against the semantic layer.

#### Page format

A knowledge page is Markdown with YAML frontmatter. `id`, `path`, and `scope` are always derived
from the filesystem path (`knowledge/global/` → global scope; `knowledge/user/<id>/` → user scope);
everything else is optional with sensible defaults:

```yaml
# knowledge/global/revenue-definition.md
---
summary: "What total_revenue means and how it is calculated."
tags: [revenue, definitions, metrics]
sl_refs:
  - warehouse_pg.orders.total_revenue   # ties this page to a live semantic entity
usage_mode: definition                   # reference | caveat | policy | definition
meta:
  provenance: human_curated
  last_validated_at: "2026-06-17T00:00:00Z"
  bound_fingerprints:
    "warehouse_pg.orders.total_revenue": "sha256:…"  # drift-detection anchor
---

The live SQL — rendered at read time, never a copy:

> `{{ sl:warehouse_pg.orders.total_revenue.expr }}`

Refunded orders are excluded by the `revenue-excludes-refunds` guardrail on every query — see
[[revenue-excludes-refunds-caveat]].
```

`{{ sl:<entity>.expr }}` directives are resolved at read time by `DefinitionRenderer` against the
live semantic layer, so the rendered definition can never fall out of sync. `[[wikilinks]]` and
`refs` / `sl_refs` in frontmatter are validated on write — a broken reference is blocked before
the page is indexed.

#### Loading and reference validation

```python
from pathlib import Path
from canon.knowledge import (
    load_knowledge_page, EntityIndex, PageIndex, ReferenceValidator,
)
from canon.semantic.loader import list_semantic_sources

root = Path(".")

# Build the entity index from live semantic sources — maps FQ names to Measure objects.
sources = list_semantic_sources(root)
entity_index = EntityIndex.from_sources(sources)

# Load a page from disk (scope, id, path derived from the filesystem path)
page = load_knowledge_page(root / "knowledge/global/revenue-definition.md")
# page.id == "revenue-definition", page.scope == KnowledgeScope.GLOBAL

# Write-time validation: every sl_ref, ref, and [[wikilink]] must resolve before indexing.
page_index = PageIndex.from_pages([page])
validator = ReferenceValidator(entity_index, page_index)
validator.validate_page(page)  # raises KnowledgeReferenceError on first broken reference
```

#### Hybrid search with caveat surfacing

`KnowledgeSearch` fuses a tantivy BM25 lexical arm with an optional vector arm via Reciprocal
Rank Fusion. The lexical arm is always available; vector search activates when an embedder is
supplied (E10). `usage_mode: caveat` pages ride along automatically when a hit references their
bound entity — no second search call needed.

```python
from canon.knowledge import KnowledgeSearch

pages = [load_knowledge_page(p) for p in (root / "knowledge" / "global").glob("*.md")]
engine = KnowledgeSearch(pages)  # lexical-only (no embedder needed)

result = engine.search("revenue", requesting_user="alice")

# Ranked hits — policy + definition pages surface alongside reference pages
for hit in result.hits:
    print(hit.page, hit.usage_mode, hit.score)
# revenue-definition    definition  0.012
# revenue-reporting-policy  policy  0.010

# Caveat pages auto-surface when a hit's sl_refs intersect their bound entities
for caveat in result.caveats:
    print(caveat.page, caveat.triggered_by)
# revenue-excludes-refunds-caveat  ['warehouse_pg.orders.total_revenue']
```

Search enforces scope visibility: every user sees `global` pages and their own `user/<id>` pages,
never another user's. When a user's page shares an `id` with a global one, the global is
authoritative and the user page rides along as a personal annotation (strict-additive rule).

#### Graph traversal

```python
from canon.knowledge import GraphTraversal, KnowledgeSearch

# After a search, expand seed hits over the reference graph (sl_refs, refs, [[wikilinks]])
traversal = GraphTraversal(pages)
subgraph = traversal.expand(result.hits, max_depth=2, max_nodes=50)
# subgraph.pages   → deduped KnowledgePage list reached by graph walk
# subgraph.entities → sorted list of sl_ref targets reached
```

#### Live rendering

```python
from canon.knowledge import DefinitionRenderer

renderer = DefinitionRenderer(entity_index)
rendered_body = renderer.render(page)
# "{{ sl:warehouse_pg.orders.total_revenue.expr }}" → "sum(amount)"
# Changing orders.yaml to expr: "sum(amount * fx_rate)" re-renders automatically, no page edit.
# An unresolvable directive is left verbatim — rendering never raises on drift.
```

#### Drift detection and freshness

```python
from canon.knowledge import DriftDetector
from datetime import timedelta

detector = DriftDetector()

# Flag pages whose bound measure expr changed since the page was authored (S6 AC2)
flagged = detector.flagged_for_review(page, entity_index)
# [] if fingerprints match; ['warehouse_pg.orders.total_revenue'] on mismatch.
# A mismatch is a prose-review signal: the rendered expr auto-updated but the surrounding
# "why" may now be wrong — resolution flows through E4's diff/review, never a silent edit.

# Surface staleness at query time (S8 AC1)
signal = detector.staleness(page, window=timedelta(days=90))
# None if validated within the window; StalenessSignal otherwise:
# StalenessSignal(page='revenue-definition', age_days=12, message='…unvalidated for 12 days')
```

The `EntityIndex` also supplies the live fingerprint of any measure, so the same object serves
drift checks, live rendering, and reference validation — no duplicate lookup.

### MCP serving surface (E8)

`canon mcp start` exposes the six P0 tools to any MCP-compatible agent client (Claude Code,
Cursor, Codex). The server is a thin adapter — all logic lives in the protocol-neutral
`CanonService`; the MCP layer only translates transport.

**Available tools:**

| Tool | Purpose |
| --- | --- |
| `contract_info` | Return the `contract_schema` version this daemon implements |
| `negotiate_contract(contract_major)` | Declare the MAJOR your client was built against; daemon rejects mismatches at connect time |
| `list_metrics` | List all active canonical metrics |
| `describe_metric(name)` | Grain, dimensions, measures, and freshness for one metric |
| `resolve_metric(name)` | Resolve a name/alias → canonical binding; surface `AMBIGUOUS`/`UNRESOLVED` |
| `compile_query(query)` | Semantic query → `{compiled: {sql, dialect}, metadata: {…}}`, no execution |
| `query(query)` | Compile + execute read-only → `QueryResult` with rows + metadata |
| `run_sql(sql, connection?)` | Execute a raw SELECT; rejects non-SELECT with `READ_ONLY_VIOLATION` |

**`query` returns a `QueryResult` with three blocks (SPEC §2.2):**

```json
{
  "result":   { "columns": [{"name": "order_date", "type": "date"},
                             {"name": "total_revenue", "type": "decimal"}],
                "rows": [["2025-01-01", 12000.50]],
                "truncated": false },
  "compiled": { "sql": "SELECT …", "dialect": "postgres" },
  "metadata": { "resolved":          {"metrics": {"revenue": "orders.total_revenue"}},
                "guardrails_fired":  [{"id": "revenue-excludes-refunds", "kind": "mandatory_filter"}],
                "freshness":         [{"source": "orders", "last_validated_at": "…", "stale": false}],
                "warnings":          [],
                "contract_schema":   "1.1" }
}
```

**`compile_query` returns a `CompileOutput` (SPEC §2.2 compile path):**

```json
{
  "compiled": { "sql": "SELECT …", "dialect": "postgres" },
  "metadata": { "resolved":         {"metrics": {"revenue": "orders.total_revenue"}},
                "guardrails_fired": [{"id": "revenue-excludes-refunds", "kind": "mandatory_filter"}],
                "freshness":        [],
                "warnings":         [],
                "contract_schema":  "1.1" }
}
```

**Structured errors** — on any `CanonError` the tool returns `{code, message, candidates?}`
instead of raising, so the agent can refuse-and-ask rather than fabricate:

```json
{ "code": "ambiguous", "message": "metric 'rev' is ambiguous",
  "candidates": [{"metric": "revenue", …}, {"metric": "revenue_gross", …}] }
```

Error codes map to the canonical registry (`UNRESOLVED` → 2, `AMBIGUOUS` → 3, …) — the
same codes the CLI uses as headless exit values, so pipeline and agent paths are identical.

**`CanonService` — the shared capability layer:**

Both the MCP adapter and the CLI capability commands (`query`, `sql`) call `CanonService`;
neither re-implements resolution or compilation. This guarantees byte-identical results
across surfaces (SPEC §2.1 adapter rule):

```python
from pathlib import Path
from canon.core.service import CanonService
from canon.compiler import SemanticQuery

service = CanonService.from_project(Path("."))

# Discovery
summaries = service.list_metrics()         # list[MetricSummary]
detail    = service.describe_metric("rev") # MetricDetail — grain, dims, freshness

# Compile only (no execution)
compiled = service.compile_query(SemanticQuery(metrics=["revenue"], dimensions=["order_date"]))
print(compiled.sql)  # deterministic, byte-identical on repeated calls

# Compile + execute
import asyncio
result = asyncio.run(service.query(SemanticQuery(metrics=["revenue"])))
print(result.result.rows)        # [[…], …]
print(result.metadata.guardrails_fired)  # [FiredGuardrailOut(id='revenue-excludes-refunds', …)]

# Raw read-only SQL
rows = asyncio.run(service.run_sql("SELECT count(*) FROM analytics.fct_orders"))
```

---

## Status & roadmap

Canon ships in phases (see [`docs/PRD-canon-final.md`](docs/PRD-canon-final.md) §9.1):

- **Phase 0 — Walking skeleton.** ✓ Complete. Foundation + install + setup wizard (E1), one primary connector (E2), compiler + minimal contracts (E5 + E15), CLI (E7), MCP serving (E8), serving contract interface freeze (P0). The serving contract is versioned as `contract_schema: v1`, locked by JSON schema golden files in CI, and stamped on every `QueryResult.metadata`. An agent or CI pipeline asks for a metric → Canon resolves the canonical binding → compiles read-only SQL with guardrail filters injected → executes against live Postgres → returns a byte-identical `QueryResult` on both CLI (`canon --json query`) and MCP (`query` tool). `canon setup` bootstraps a new project interactively with checkpoint-based resumability.
- **Phase 1 — v1 core (in progress).** `canon ingest` (E4) is live: four-stage deterministic pipeline (builder → reconciliation → validation → diff emission), headless CI mode with auto-PR, `--strict` gate, full exit-code coverage. Serving contract bumped to `contract_schema: 1.1` (non-breaking). Knowledge & retrieval (E6) is substantially complete: page schema + loader (GH-46), reference graph with write-time validation + ingest-time pruning (GH-47/48), scope visibility + strict-additive collisions (GH-49), hybrid BM25 + vector search with RRF fusion (GH-50), graph traversal (GH-51), and drift/freshness/usage_mode — live rendering, review flags, caveat surfacing (GH-52). Remaining Phase 1: LLM config incl. local/offline (E10), definition + evidence connectors (E3), fuller contracts, accuracy tracking.
- **Phase 2 — Trust & operations.** Cost control (E13), answer trust score (E14), feedback loop (E11), agent edit/review loop (E9), governance: RLS/PII + locked context versions (E12).

## Documentation

- [`docs/PRD-canon-final.md`](docs/PRD-canon-final.md) — full product requirements
- [`docs/SPEC-E1-foundation-config-distribution.md`](docs/SPEC-E1-foundation-config-distribution.md) — project foundation, config, distribution
- [`docs/SPEC-E2-primary-source-connector.md`](docs/SPEC-E2-primary-source-connector.md) — primary source connector
- [`docs/SPEC-E5-E15-semantics-and-contracts.md`](docs/SPEC-E5-E15-semantics-and-contracts.md) — semantic layer, compiler & contract surface
- [`docs/SPEC-E6-knowledge-retrieval.md`](docs/SPEC-E6-knowledge-retrieval.md) — knowledge pages, reference graph, hybrid retrieval, drift & freshness
- [`docs/SPEC-E7-E8-serving-surfaces.md`](docs/SPEC-E7-E8-serving-surfaces.md) — CLI & MCP serving surfaces
- [`docs/SPEC-P0-interface-freeze.md`](docs/SPEC-P0-interface-freeze.md) — serving contract version policy and conformance gate
