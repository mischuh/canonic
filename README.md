# canonic

[![CI](https://github.com/mischuh/canonic/actions/workflows/ci.yml/badge.svg)](https://github.com/mischuh/canonic/actions/workflows/ci.yml)

**The context layer that lets AI agents query your data correctly.**

Point canonic at your database and it builds the context an agent needs to answer data questions accurately — definitions, relationships, business meaning, and the guardrails that stop confidently-wrong answers. It keeps that context up to date as your data changes, and it never touches your warehouse beyond reading it.

> Package and image names below show the shape of each install channel; exact names are confirmed per release.

---

## The problem

An AI agent connected straight to your warehouse sees **tables and columns** — not **meaning**. It doesn't know that `revenue` lives in `orders.amount` but excludes refunds, that "active customer" has a specific definition your finance team agreed on, or that summing a daily balance across a week is nonsense. So it guesses. The result is the worst kind of wrong: a confident, well-formatted, *incorrect* number that looks right.

Schema access makes an agent *fluent*. It doesn't make it *correct*.

**❌ Without canonic (the guess):**
```
Agent query: SELECT SUM(amount) FROM orders;
Result: $1,450,320

The sum includes refunded orders and sales tax, making the number wrong
by 12%. The agent delivers a confident, well-formatted, and completely
incorrect metric.
```

**✅ With canonic (the truth):**
```
Agent request: "Give me revenue"
Canonic compiles: SELECT SUM(amount) FROM orders
                  WHERE status = 'completed' AND type != 'tax';
Result: $1,274,500 [certified fresh, excludes refunds and tax]

The agent gets the exact right number, with the business logic and
caveats baked in — and it knows the data freshness and which
guardrails applied. Zero guessing.
```

## What canonic does

canonic sits between your data and your agents as a **context layer**: an auto-built, auto-maintained, version-controlled description of what your data *means* and how to query it *safely*. Agents ask for a metric by name; canonic resolves it to the canonical definition, compiles correct read-only SQL, runs it, and returns the answer **with the caveats that make it trustworthy** — how fresh the data is, which guardrails applied, whether the number is final or provisional.

When canonic isn't sure, it **refuses and asks** instead of guessing. A confidently-wrong answer is the one outcome it's built to never produce.

---

## Why canonic, not something else

| Instead of… | You get… | canonic gives you… |
| --- | --- | --- |
| Giving the agent raw schema/SQL access | Fluency without correctness — it guesses definitions and picks wrong tables | Resolved canonical definitions, enforced guardrails, never a silent wrong number |
| Hand-building a semantic layer from scratch | Months of modeling before any value | Context auto-drafted from your live schema on day one; you review, not author from zero |
| Migrating onto a new metrics platform | Lock-in and a rebuild | canonic **ingests** your dbt / BI tools / docs — it feeds your existing stack, it doesn't replace it |
| A hosted "AI analytics" SaaS | Your data and definitions leaving your environment | Local-first, fully **air-gapped-capable** — nothing has to leave your machine |

What makes it different in one line: **canonic builds the context for you, keeps it honest, and refuses to lie when it isn't sure.**

---

## The three layers

canonic's context lives in three committed surfaces — plain files in your git repo, reviewed like code. Each answers a different question.

| Layer | File | Answers | Owned by |
| --- | --- | --- | --- |
| **Semantics** | `semantics/**/*.yaml` | "How do I query this safely?" — tables, types, grains, joins, measures | auto-maintained |
| **Knowledge** | `knowledge/**/*.md` | "What does this mean to the business?" — definitions, caveats, policies | auto-maintained |
| **Contracts** | `contracts/**/*.yaml` | "Which definition is canonical, and what must the answer obey?" | human-owned |

**The split rule:**
- Changes how the SQL *runs* → **semantics**.
- A human needs it to *trust* the answer → **knowledge**.
- Governs *which* definition is authoritative or *what an answer must satisfy* → **contracts**.

The key idea: a knowledge page *explains* why "amount includes refunds unless filtered." A contract *makes the SQL obey* it. Documented caveats become enforced guardrails — so the warning can't be silently ignored.

---

## Out of the box vs. a bit more effort

**Works immediately, zero modeling:**
- Connect a database (SQLite or Postgres) and canonic introspects the live schema.
- It auto-drafts semantics — typed columns, primary-key grains, foreign-key joins, and additive measures (sums, counts).
- The setup wizard ends by **answering a real question from your data**, so you see the payoff in minutes.
- Connect your agent over MCP and start asking.

**A bit more manual effort (when you need it):**
- **Canonical bindings** — when two sources define "revenue" differently, you pick the authoritative one. canonic surfaces the ambiguity; you resolve it once.
- **Knowledge prose** — the business "why" behind a definition; canonic drafts it, you refine it.
- **Guardrails & contracts** — mandatory filters, required dimensions, final-vs-provisional rules. Added when a number needs protecting.
- **Non-additive metrics** — ratios, averages, distinct counts, balances. Declared as composable definitions so they stay correct at any grain.
- **More sources** — dbt / Metabase / Notion / web pages, layered on as context evidence. The connector contract is extensible, so a Confluence, Jira, or other wiki/knowledge-base connector can be added the same way.

The design principle throughout: **canonic proposes, you approve.** It never silently edits your context — every change is a reviewable diff.

---

## Install

> canonic ships as one distributable (CLI + local daemon). Local embeddings are an optional add-on.

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

**Offline / air-gapped install** — no outbound network calls during install:
```bash
uv pip install --no-index --find-links ./wheels canonic
```

Verify:
```bash
canonic --version
```

---

## Quickstart — your first answer in minutes

The fastest path uses **local connectors** — no server, no network. Pick either SQLite or DuckDB:

**SQLite** — a local `.db` file:
```bash
canonic setup
# Point at a .db file when prompted
```

**DuckDB** — a local `.duckdb` file or data files (CSV/Parquet/JSON):
```bash
canonic setup
# Point at a .duckdb file, or let it read a CSV/Parquet directly
```

![canonic setup end-to-end on the vehicle rental example](docs/demo_canonic_setup.gif)

The wizard walks you through:
1. **Name** your project.
2. **Connect** a source — SQLite/DuckDB file, or Postgres if you have a server.
3. **Configure an LLM** — optional, and skippable. The core works without one.
4. **Bootstrap** — canonic introspects the schema and drafts your semantics.
5. **First answer** — the wizard runs a real query against your data and shows the result, plus how fresh it is and which definition it used.

Don't have a database handy? `examples/` ships 5 ready-to-run sample projects — dbt Jaffle Shop, e-commerce, vehicle rental, SaaS analytics, and Dutch railway — a good way to try the wizard before pointing it at your own data. See the [guides](docs/guides/) for the walkthrough behind each one, e.g. [vehicle rental](docs/guides/rental.mdx).

You now have a working context layer committed to your repo. Ask your own questions:
```bash
canonic query --metrics revenue --dimensions order_date
canonic query --metrics revenue --filter "status=paid"
```

For a query with more filters or joins than is comfortable inline, write a `SemanticQuery` JSON file and pass it with `-f` instead — see [`canonic query`](docs/cli-reference/query-sql-assert.mdx) for the full flag reference.

> `canonic knowledge search` is not implemented yet (returns a "not implemented" notice); see [CLI reference: knowledge](docs/cli-reference/knowledge.mdx) for its current status. `canonic knowledge add <url>` (fetch-and-write a page) works today.

Review what canonic drafted when you're ready — it's all an ordinary git diff:
```bash
canonic review
canonic status        # always tells you the best next step
```

---

## Configuring an LLM

An LLM is **optional** — canonic's answer path is fully deterministic and never calls one. It's only used to *draft* semantics/knowledge during setup and reconciliation. The `llm:` block in `canonic.yaml` supports four providers, all behind the same interface:

**`openai_compatible`** — local runtimes (Ollama, vLLM, LM Studio, llama.cpp, TGI) or any hosted OpenAI-compatible endpoint. `base_url` is required; a key is optional (local servers typically need none):
```yaml
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:11434/v1   # Ollama; swap for any OpenAI-compatible endpoint
  model: gemma-4-e2b-it-4bit
  api_key_ref: env:CANONIC_LLM_API_KEY   # optional
```

**`anthropic`** — Claude, called directly. No `base_url` needed; `api_key_ref` is required:
```yaml
llm:
  provider: anthropic
  model: claude-opus-4-8
  api_key_ref: env:ANTHROPIC_API_KEY
```

**`openai`** — OpenAI's hosted API directly (not a self-hosted/compatible endpoint). No `base_url` needed; `api_key_ref` is required:
```yaml
llm:
  provider: openai
  model: gpt-4o
  api_key_ref: env:OPENAI_API_KEY
```

**`github_copilot`** — routed through your GitHub Copilot subscription. No `base_url`, and **no `api_key_ref`** — authentication is a one-time device-code flow (a browser prompt on first use); the resulting credential is cached on disk and reused after that:
```yaml
llm:
  provider: github_copilot
  model: gpt-4.1
```
> Structured/schema-constrained output (used for drafting) isn't honored by every model Copilot proxies — Claude and most GPT models silently return prose instead of JSON. Stick to a model litellm marks as schema-capable for this provider (`gpt-4.1`, `gpt-5`, `gpt-5.1`, `gpt-5.2`) if you hit `structured_output_unsupported`.

All four are reached through [litellm](https://github.com/BerriAI/litellm) behind one interface — no per-provider branching anywhere else in canonic. `tasks:` optionally overrides the model per task (`draft`, `reconcile`):
```yaml
llm:
  provider: anthropic
  model: claude-haiku-4-5
  api_key_ref: env:ANTHROPIC_API_KEY
  tasks:
    reconcile: claude-opus-4-8   # a harder task gets a stronger model
```

Under `runtime.air_gapped: true`, only a local endpoint (loopback, or an allowlisted host via `runtime.allow_cidrs`) is accepted — `openai`, `anthropic`, and `github_copilot` all call a fixed public endpoint and are rejected outright in that mode.

---

## Connect your agent (MCP)

canonic exposes its capabilities to agent clients through a **local, on-demand MCP server** — no always-on hosted service. Verified with **Claude Code, Cursor, and Codex**.

**1. Start the daemon** (binds locally; reads your committed context):
```bash
canonic mcp start
canonic mcp status
```

**2. Register canonic in your client's MCP config.** A standard MCP server entry, for example:
```json
{
  "mcpServers": {
    "canonic": {
      "command": "canonic",
      "args": ["mcp", "start"]
    }
  }
}
```
(See the per-client docs for the exact config location — Claude Code, Cursor, and Codex each load standard MCP configuration.)

If you started the daemon with `--http` instead (a background daemon on a fixed host/port, useful when the client can't spawn a process), point your client at the HTTP endpoint:
```bash
canonic mcp start --http --host 127.0.0.1 --port 7474
```
```json
{
  "mcpServers": {
    "canonic": {
      "url": "http://127.0.0.1:7474/mcp"
    }
  }
}
```
Adjust host/port to match the flags used to start the daemon — see [`canonic mcp`](docs/cli-reference/mcp.mdx) for the full flag reference.

**3. Your agent now has these tools** ([full reference](docs/mcp-integration/tools-reference.mdx)):

| Tool | What the agent does with it |
| --- | --- |
| `contract_info` | check the serving contract version at session start |
| `negotiate_contract` | declare the contract-schema major version the client expects |
| `get_overview` | recommended first call — active metrics grouped by domain with sample questions |
| `list_metrics` | discover what it can ask for |
| `describe_metric` | grain, dimensions, owning source, freshness |
| `resolve_metric` | check a name resolves; surface ambiguity instead of guessing |
| `compile_query` | get the SQL + metadata without running it |
| `query` | the main path — answer a question, with caveats attached |
| `run_sql` | read-only SQL escape hatch |
| `search_knowledge` | find knowledge and definitions by text |
| `read_knowledge_page` | read a full knowledge page to relay its explanation |

Every answer comes back with the **metadata band** — resolved definition, guardrails fired, freshness, final/provisional — so the agent can caveat honestly. On ambiguity or a blocked guardrail, the tool returns the candidates or the rationale, and the agent **asks** rather than fabricates.

---

## What you can rely on

- **Read-only.** canonic never mutates your warehouse. It reads, it never writes back.
- **Propose-only.** It never silently edits your context — every change is a reviewable diff anchored to evidence.
- **Refuse-and-ask.** Ambiguous or unsafe? It returns a structured reason, not a guess.
- **No LLM in the answer path.** Queries compile deterministically — the same question always produces the same SQL. An LLM only helps *draft* context, never *compute* an answer.
- **Local-first & air-gapped-capable.** Run entirely on your machine with a local model and local embeddings; nothing has to leave your network.
- **Measurable.** A local event log tracks accuracy, freshness, and answer quality — so "trustworthy" is something you can check, not just claim.

---

## Where to go next

- **[Concepts](docs/concepts/three-layers.mdx)** — the three layers and the split rule (the one mental model worth learning first).
- **[CLI reference](docs/cli-reference/overview.mdx)** — every command, flag by flag.
- **[MCP / agent integration](docs/mcp-integration/connecting-your-agent.mdx)** — wiring canonic into Claude Code, Cursor, Codex, or any MCP client.
- **[Guides](docs/guides/)** — 5 ready-to-run example projects: [Jaffle Shop](docs/guides/jaffle-shop.mdx), [e-commerce](docs/guides/ecommerce.mdx), [vehicle rental](docs/guides/rental.mdx), [SaaS analytics](docs/guides/saas-analytics.mdx), [Dutch railway](docs/guides/dutch-railway.mdx).
- **[Reference](docs/reference/error-codes.mdx)** — error codes and the full `canonic.yaml` config schema.

canonic is local-first, git-native, and read-only by design. Start with a SQLite file and one question; grow into the full context layer as your needs do.
