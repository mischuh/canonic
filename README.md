# canonic

<!-- mcp-name: io.github.mischuh/canonic -->

[![CI](https://github.com/mischuh/canonic/actions/workflows/ci.yml/badge.svg)](https://github.com/mischuh/canonic/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/canonic)](https://pypi.org/project/canonic/)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue)](https://github.com/mischuh/canonic/blob/main/LICENSE.md)

**The context layer that lets AI agents query your data correctly.**

Point canonic at your database and it builds the context an agent needs to answer data questions accurately: definitions, relationships, business meaning, and the guardrails that stop confidently-wrong answers. It keeps that context up to date as your data changes, and it never touches your warehouse beyond reading it.

📖 Full documentation: **https://docs.getcanonic.app**

## The problem

An AI agent connected straight to your warehouse sees **tables and columns**, not **meaning**. It doesn't know that `revenue` lives in `orders.amount` but excludes refunds, or that "active customer" has a specific definition your finance team agreed on. So it guesses. Schema access makes an agent *fluent*. It doesn't make it *correct*.

Real output, captured from a live run against the [ecommerce example](https://github.com/mischuh/canonic/tree/main/examples/ecommerce):

```bash
$ canonic sql "SELECT SUM(amount) FROM fct_orders"
┏━━━━━━━━━┓
┃ sum     ┃
┡━━━━━━━━━┩
│ 4050.50 │
└─────────┘
```
This total includes two refunded orders ($260), a confident, well-formatted number that's off by 6.4%.

```bash
$ canonic --json query --metrics revenue
{
  "result": { "rows": [["3790.50"]] },
  "compiled": {
    "sql": "SELECT SUM(\"orders\".\"amount\") AS \"total_revenue\" FROM \"fct_orders\" AS \"orders\" WHERE \"orders\".\"status\" <> 'refunded'"
  },
  "metadata": {
    "guardrails_fired": [{ "id": "revenue-excludes-refunds", "kind": "mandatory_filter" }]
  }
}
```

canonic resolves "revenue" to its canonical definition, compiles the guardrail into the SQL whether or not anyone asked for it, and returns the right number with the reasoning attached.

canonic is not a BI tool and not a chat interface: it's the layer that feeds the tools you already have (a BI dashboard, an agent, a notebook) correct, governed answers.

## What canonic does

- **Canonical definitions, enforced.** An agent asks for a metric by name. canonic resolves it to the one agreed definition and compiles the SQL. Guardrails such as `mandatory_filter`, `required_dimension`, `restrict_source` and `min_trust` are part of the contract, so a documented caveat can't be silently skipped. [Contracts and guardrails](https://docs.getcanonic.app/concepts/contracts-and-guardrails)
- **The right aggregation, not just valid SQL.** Semi-additive measures (MRR snapshots), ratios recomputed at the requested grain, distinct counts and percentiles each get their own compilation strategy, and joins that would fan out a fact table are caught. Several valid join paths? You pick one with `via`, canonic doesn't guess. [Compiler](https://docs.getcanonic.app/concepts/compiler)
- **Answers carry their trust.** Every result ships a metadata band: the resolved definition, guardrails fired, freshness, final vs. provisional rows, and a trust tier (`trusted`, `provisional`, `caution`) with the reasons behind it. Assertions gate CI, and confirmed-wrong outcomes lower a metric's trust. [Trust and assertions](https://docs.getcanonic.app/concepts/contracts-and-guardrails)
- **Refuse and ask.** Ambiguous or unsafe questions come back as a structured reason (`ambiguous_join_path`, `guardrail_block`, ...) the agent can act on, never as a guess. [Error codes](https://docs.getcanonic.app/reference/error-codes)
- **The context builds itself.** Ingest drafts semantics from your live schema, a dbt manifest or an [Apache Ossie](https://ossie.apache.org/) model, and learns from Looker and Metabase usage, Notion pages and web docs. Every change is a reviewable diff (`canonic review`, `canonic apply`), drift is flagged, and references to things that disappeared are proposed for removal. [Ingestion](https://docs.getcanonic.app/concepts/ingestion-and-reconciliation)
- **Business knowledge for agents.** Definitions, policies and caveats live as Markdown pages. Agents search them, read them with live-rendered definitions, and get the relevant caveats attached to an answer. [Knowledge layer](https://docs.getcanonic.app/concepts/knowledge-layer)
- **Governance built in.** Tenant scoping, role-based access, column masking, and OAuth 2.1 / OIDC for the MCP server, including identity assertion for agents acting on behalf of a user. [Tenancy and access control](https://docs.getcanonic.app/concepts/tenancy-and-access-control)
- **Curated reports and context packs.** Commit a report once and run it by name. Install a pack for a known system (PostHog is the first) and get working metrics, guardrails and a first answer. [Reports](https://docs.getcanonic.app/cli-reference/report), [packs](https://docs.getcanonic.app/cli-reference/pack)
- **Your warehouse, read-only.** Postgres, Redshift, MySQL, Snowflake, Databricks, ClickHouse, SQLite and DuckDB (including CSV and Parquet files). [Connectors](https://docs.getcanonic.app/concepts/connectors)
- **Measurable.** A local event log feeds `canonic audit` and `canonic status`, and an accuracy harness (`canonic assert`, `canonic eval baseline`) turns "trustworthy" into something you can check. [Instrumentation](https://docs.getcanonic.app/concepts/instrumentation-and-eval)

## How it works

```
 your sources                 canonic                          consumers
 ────────────                 ───────                          ─────────
 warehouse schema ┐   ingest  ┌──────────────────────────┐     CLI (query, sql, report)
 dbt / Ossie      ├─────────▶ │ semantics/  knowledge/   │ ──▶ MCP server (13 tools)
 Looker, Metabase │  propose  │ contracts/  (files in git)│     Claude Code, Cursor, Codex,
 Notion, web docs ┘  → review └──────────────────────────┘     any MCP client
```

A question takes one deterministic path: resolve the metric, compile SQL against the canonical definition, apply guardrails, run it read-only, attach the metadata band. No LLM is involved at answer time.

## What an agent gets back

Real output (abridged) from `canonic --json query --metrics gross_revenue` in the [saas-analytics example](https://github.com/mischuh/canonic/tree/main/examples/saas-analytics):

```json
{
  "result": { "columns": [{ "name": "total_amount", "type": "decimal" }], "rows": [["77071.00"]] },
  "compiled": {
    "sql": "SELECT SUM(\"fct_invoices\".\"amount\") AS \"total_amount\" FROM \"fct_invoices\" AS \"fct_invoices\" WHERE \"fct_invoices\".\"status\" <> 'refunded' AND \"fct_invoices\".\"is_trial\" = FALSE",
    "dialect": "duckdb"
  },
  "metadata": {
    "resolved": { "metrics": { "gross_revenue": "fct_invoices.total_amount" } },
    "guardrails_fired": [
      { "id": "revenue-excludes-refunds", "kind": "mandatory_filter", "severity": "error" },
      { "id": "revenue-excludes-trials", "kind": "mandatory_filter", "severity": "error" }
    ],
    "freshness": [{ "source": "fct_invoices", "stale": false }],
    "trust_score": {
      "tier": "provisional",
      "reasons": ["gross_revenue: assertion unverified (pass/fail not yet persisted)"]
    }
  }
}
```

The agent sees which definition answered, which rules shaped the SQL, and why the number is only `provisional`, so it can caveat the answer instead of presenting it as settled.

## The three layers

canonic's context lives in three committed surfaces: plain files in your git repo, reviewed like code.

| Layer | File | Answers | Owned by |
| --- | --- | --- | --- |
| **Semantics** | `semantics/**/*.yaml` | "How do I query this safely?" | auto-maintained |
| **Knowledge** | `knowledge/**/*.md` | "What does this mean to the business?" | auto-maintained |
| **Contracts** | `contracts/**/*.yaml` | "Which definition is canonical, and what must the answer obey?" | human-owned |

Changes how the SQL *runs* → semantics. A human needs it to *trust* the answer → knowledge. Governs *which* definition is authoritative → contracts. See [Concepts: the three layers](https://docs.getcanonic.app/concepts/three-layers).

## Quickstart

canonic needs Python 3.13 or newer (`uv` fetches it for you).

```bash
uvx canonic --version        # try it without installing
uv tool install canonic      # persistent, global command
pip install canonic          # without uv
docker pull ghcr.io/mischuh/canonic:latest   # CI, headless, air-gapped
```

The fastest path uses local connectors, no server, no network. Point at a SQLite `.db` or DuckDB `.duckdb`/CSV/Parquet file:

```bash
canonic setup
```

![canonic setup end-to-end on the vehicle rental example](https://raw.githubusercontent.com/mischuh/canonic/main/docs/demo_canonic_setup.gif)

The wizard names your project, connects a source, optionally configures an LLM, drafts your semantics from the live schema, then runs a real query and shows the answer with its freshness and definition. A server-based database or an LLM provider needs a credential in an environment variable (or a `file:` reference) *before* you run `canonic setup`, because canonic never stores secrets in `canonic.yaml` directly.

You now have a working context layer committed to your repo:

```bash
canonic overview                                           # what's askable
canonic query --metrics revenue --dimensions order_date    # ask it
canonic review && canonic status                           # review what it drafted
```

Air-gapped install and offline wheels: [Installation](https://docs.getcanonic.app/installation).

## Connect your agent (MCP)

canonic exposes its 13 tools over a local, on-demand MCP server, verified with **Claude Code, Cursor, and Codex**:

```bash
canonic mcp start
```

```json
{
  "mcpServers": {
    "canonic": {
      "command": "uvx",
      "args": ["canonic", "mcp", "start", "--project", "/path/to/canonic/examples/rental", "--suggestions"]
    }
  }
}
```

GUI-launched clients (Claude Desktop, Cursor) don't source your shell profile, so pass connection credentials via the config's `env` field, not `export`.

See [Connecting your agent](https://docs.getcanonic.app/mcp-integration/connecting-your-agent) for remote and enterprise deployment (`--transport http`, per-client bearer tokens) and the [tools reference](https://docs.getcanonic.app/mcp-integration/tools-reference). To see masking, `run_sql` gating and tenancy scoping applied to a real identity, [`scripts/local_idp`](https://github.com/mischuh/canonic/tree/main/scripts/local_idp) runs the marketplace example behind a local Keycloak: [walkthrough](https://docs.getcanonic.app/guides/marketplace-keycloak).

## Example projects

`examples/` ships seven ready-to-run projects, each with a [guide](https://docs.getcanonic.app/guides/jaffle-shop):

| Example | Backend | Shows |
| --- | --- | --- |
| [jaffle-shop](https://github.com/mischuh/canonic/tree/main/examples/jaffle-shop) | DuckDB | dbt manifest with MetricFlow models as evidence |
| [ecommerce](https://github.com/mischuh/canonic/tree/main/examples/ecommerce) | Postgres | the full loop: guardrails, finality, Notion and dbt evidence, observability |
| [rental](https://github.com/mischuh/canonic/tree/main/examples/rental) | SQLite | cross-fact fanout and a NULL-handling guardrail, no setup needed |
| [saas-analytics](https://github.com/mischuh/canonic/tree/main/examples/saas-analytics) | DuckDB | every metric binding kind, all guardrail kinds, finality, assertions |
| [dutch-railway](https://github.com/mischuh/canonic/tree/main/examples/dutch-railway) | DuckDB | a geography dimension chain and ratio metrics |
| [marketplace](https://github.com/mischuh/canonic/tree/main/examples/marketplace) | SQLite | tenant scoping, role-based access and column masking |
| [ossie-retail](https://github.com/mischuh/canonic/tree/main/examples/ossie-retail) | SQLite | context bootstrapped from an Apache Ossie model |

## What you can rely on

- **Read-only.** canonic never mutates your warehouse.
- **Propose-only, refuse-and-ask.** Every change is a reviewable diff; ambiguous or unsafe answers get a structured reason, not a guess.
- **No LLM in the answer path.** Queries compile deterministically. An LLM is optional and only *drafts* context during setup, four providers supported (Anthropic, OpenAI, any OpenAI-compatible endpoint, GitHub Copilot), see [Configuring an LLM](https://docs.getcanonic.app/configuring-an-llm).
- **Local-first & air-gapped-capable.** Run entirely on your machine; nothing has to leave your network.

## For developers

```bash
uv sync                          # install with dev dependencies
uv run pytest tests/ -x          # tests, including the golden suite
uv run ruff check . && uv run ruff format --check .
uv run mypy canonic/
```

- **Layout.** `canonic/` is the package (compiler, connectors, contracts, ingestion, knowledge, MCP server, trust, instrumentation), `tests/` mirrors it, `examples/` holds the sample projects.
- **Extension points.** Connectors register with the `ConnectorFactory` by capability (`introspect_schema`, `run_read_only_sql`, `extract_definitions`, `extract_evidence`), and short-lived credentials plug in through the `CredentialProviderRegistry`. Context packs live in the separate [canonic-packs](https://github.com/mischuh/canonic-packs) repo.
- **Stable contract.** The serving contract is versioned (the `contract_info` MCP tool, [`CONTRACT_CHANGELOG.md`](https://github.com/mischuh/canonic/blob/main/CONTRACT_CHANGELOG.md)), and `tests/golden/` locks compiled SQL and executed numbers for the examples.
- **Contributing.** Conventional Commits, the golden-suite workflow and the contract-change process are in [CONTRIBUTING.md](https://github.com/mischuh/canonic/blob/main/CONTRIBUTING.md).

## Documentation

- **[Quickstart](https://docs.getcanonic.app/quickstart)**: first answer in minutes.
- **[Concepts](https://docs.getcanonic.app/concepts/three-layers)**: the three layers, the compiler, contracts, tenancy.
- **[CLI reference](https://docs.getcanonic.app/cli-reference/overview)**: every command, flag by flag.
- **[MCP / agent integration](https://docs.getcanonic.app/mcp-integration/connecting-your-agent)**: wiring canonic into Claude Code, Cursor, Codex, or any MCP client.
- **[Guides](https://docs.getcanonic.app/guides/jaffle-shop)**: the seven example projects and the warehouse walkthroughs.
- **[Reference](https://docs.getcanonic.app/reference/error-codes)**: error codes and the full `canonic.yaml` config schema.

## License

[Business Source License 1.1](https://github.com/mischuh/canonic/blob/main/LICENSE.md). Free for non-commercial, personal, evaluation, educational and developmental use. Commercial use needs a separate license. The licensed version converts to Apache 2.0 on July 17, 2030.
