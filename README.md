# canonic

[![CI](https://github.com/mischuh/canonic/actions/workflows/ci.yml/badge.svg)](https://github.com/mischuh/canonic/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/canonic)](https://pypi.org/project/canonic/)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue)](LICENSE.md)

**The context layer that lets AI agents query your data correctly.**

Point canonic at your database and it builds the context an agent needs to answer data questions accurately: definitions, relationships, business meaning, and the guardrails that stop confidently-wrong answers. It keeps that context up to date as your data changes, and it never touches your warehouse beyond reading it.

📖 Full documentation: **https://docs.getcanonic.app**

> Package and image names below show the shape of each install channel; exact names are confirmed per release.

## The problem

An AI agent connected straight to your warehouse sees **tables and columns**, not **meaning**. It doesn't know that `revenue` lives in `orders.amount` but excludes refunds, or that "active customer" has a specific definition your finance team agreed on. So it guesses. Schema access makes an agent *fluent*. It doesn't make it *correct*.

Real output, captured from a live run against the [ecommerce example](examples/ecommerce):

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

## The three layers

canonic's context lives in three committed surfaces: plain files in your git repo, reviewed like code.

| Layer | File | Answers | Owned by |
| --- | --- | --- | --- |
| **Semantics** | `semantics/**/*.yaml` | "How do I query this safely?" | auto-maintained |
| **Knowledge** | `knowledge/**/*.md` | "What does this mean to the business?" | auto-maintained |
| **Contracts** | `contracts/**/*.yaml` | "Which definition is canonical, and what must the answer obey?" | human-owned |

Changes how the SQL *runs* → semantics. A human needs it to *trust* the answer → knowledge. Governs *which* definition is authoritative → contracts. See [Concepts: the three layers](https://docs.getcanonic.app/concepts/three-layers).

## Install

**uv** (dev machines, primary):
```bash
uvx canonic --version        # ephemeral, no install step
uv tool install canonic      # persistent, global command
```

**pip** (fallback for environments without `uv`):
```bash
pip install canonic
```

**Docker** (CI, headless, air-gapped):
```bash
docker pull ghcr.io/mischuh/canonic:latest
```

Verify with `canonic --version`. Air-gapped install and offline wheels: see [Installation](https://docs.getcanonic.app/installation).

## Quickstart

The fastest path uses local connectors, no server, no network. Point at a SQLite `.db` or DuckDB `.duckdb`/CSV/Parquet file:

```bash
canonic setup
```

![canonic setup end-to-end on the vehicle rental example](https://raw.githubusercontent.com/mischuh/canonic/main/docs/demo_canonic_setup.gif)

The wizard names your project, connects a source, optionally configures an LLM, drafts your semantics from the live schema, then runs a real query and shows the answer with its freshness and definition. Postgres or an LLM provider need a credential in an environment variable *before* you run `canonic setup` (canonic never stores secrets in `canonic.yaml` directly).

Don't have a database handy? `examples/` ships 5 ready-to-run sample projects (dbt Jaffle Shop, e-commerce, vehicle rental, SaaS analytics, Dutch railway), see the [guides](https://docs.getcanonic.app/guides/jaffle-shop).

You now have a working context layer committed to your repo:
```bash
canonic overview                                           # what's askable
canonic query --metrics revenue --dimensions order_date    # ask it
canonic review && canonic status                           # review what it drafted
```

## Connect your agent (MCP)

canonic exposes its capabilities over a local, on-demand MCP server, verified with **Claude Code, Cursor, and Codex**:

```bash
canonic mcp start
```

```json
{
  "mcpServers": {
    "canonic": {
      "command": "uvx",
      "args": [
        "canonic",
        "mcp",
        "start",
        "--project",
        "/path/to/canonic/examples/rental",
        "--suggestions"
      ]
    }
  }
}
```

GUI-launched clients (Claude Desktop, Cursor) don't source your shell profile, so pass connection credentials via the config's `env` field, not `export`. Every answer-producing tool of the 11 registered (`query`, `run_sql`, `search_knowledge`, ...) returns a metadata band: resolved definition, guardrails fired, freshness, `trust_score`. On ambiguity, the agent gets a structured reason, not a guess.

See [Connecting your agent](https://docs.getcanonic.app/mcp-integration/connecting-your-agent) for remote/enterprise deployment (`--transport http`, per-client bearer tokens) and the [tools reference](https://docs.getcanonic.app/mcp-integration/tools-reference).

## What you can rely on

- **Read-only.** canonic never mutates your warehouse.
- **Propose-only, refuse-and-ask.** Every change is a reviewable diff; ambiguous or unsafe answers get a structured reason, not a guess.
- **No LLM in the answer path.** Queries compile deterministically. An LLM is optional and only *drafts* context during setup, four providers supported (Anthropic, OpenAI, any OpenAI-compatible endpoint, GitHub Copilot), see [Configuring an LLM](https://docs.getcanonic.app/configuring-an-llm).
- **Local-first & air-gapped-capable.** Run entirely on your machine; nothing has to leave your network.

## Documentation

- **[Quickstart](https://docs.getcanonic.app/quickstart)**: first answer in minutes.
- **[Concepts](https://docs.getcanonic.app/concepts/three-layers)**: the three layers and the split rule.
- **[CLI reference](https://docs.getcanonic.app/cli-reference/overview)**: every command, flag by flag.
- **[MCP / agent integration](https://docs.getcanonic.app/mcp-integration/connecting-your-agent)**: wiring canonic into Claude Code, Cursor, Codex, or any MCP client.
- **[Guides](https://docs.getcanonic.app/guides/jaffle-shop)**: 5 ready-to-run example projects.
- **[Reference](https://docs.getcanonic.app/reference/error-codes)**: error codes and the full `canonic.yaml` config schema.

## License

[Business Source License 1.1](LICENSE.md).
