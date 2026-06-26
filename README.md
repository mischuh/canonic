# canon

**The context layer that lets AI agents query your data correctly.**

Point canon at your database and it builds the context an agent needs to answer data questions accurately — definitions, relationships, business meaning, and the guardrails that stop confidently-wrong answers. It keeps that context up to date as your data changes, and it never touches your warehouse beyond reading it.

> Package and image names below show the shape of each install channel; exact names are confirmed per release.

---

## The problem

An AI agent connected straight to your warehouse sees **tables and columns** — not **meaning**. It doesn't know that `revenue` lives in `orders.amount` but excludes refunds, that "active customer" has a specific definition your finance team agreed on, or that summing a daily balance across a week is nonsense. So it guesses. The result is the worst kind of wrong: a confident, well-formatted, *incorrect* number that looks right.

Schema access makes an agent *fluent*. It doesn't make it *correct*.

## What canon does

canon sits between your data and your agents as a **context layer**: an auto-built, auto-maintained, version-controlled description of what your data *means* and how to query it *safely*. Agents ask for a metric by name; canon resolves it to the canonical definition, compiles correct read-only SQL, runs it, and returns the answer **with the caveats that make it trustworthy** — how fresh the data is, which guardrails applied, whether the number is final or provisional.

When canon isn't sure, it **refuses and asks** instead of guessing. A confidently-wrong answer is the one outcome it's built to never produce.

---

## Why canon, not something else

| Instead of… | You get… | canon gives you… |
| --- | --- | --- |
| Giving the agent raw schema/SQL access | Fluency without correctness — it guesses definitions and picks wrong tables | Resolved canonical definitions, enforced guardrails, never a silent wrong number |
| Hand-building a semantic layer from scratch | Months of modeling before any value | Context auto-drafted from your live schema on day one; you review, not author from zero |
| Migrating onto a new metrics platform | Lock-in and a rebuild | canon **ingests** your dbt / LookML / docs — it feeds your existing stack, it doesn't replace it |
| A hosted "AI analytics" SaaS | Your data and definitions leaving your environment | Local-first, fully **air-gapped-capable** — nothing has to leave your machine |

What makes it different in one line: **canon builds the context for you, keeps it honest, and refuses to lie when it isn't sure.**

---

## The three layers

canon's context lives in three committed surfaces — plain files in your git repo, reviewed like code. Each answers a different question.

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
- Connect a database (SQLite or Postgres) and canon introspects the live schema.
- It auto-drafts semantics — typed columns, primary-key grains, foreign-key joins, and additive measures (sums, counts).
- The setup wizard ends by **answering a real question from your data**, so you see the payoff in minutes.
- Connect your agent over MCP and start asking.

**A bit more manual effort (when you need it):**
- **Canonical bindings** — when two sources define "revenue" differently, you pick the authoritative one. canon surfaces the ambiguity; you resolve it once.
- **Knowledge prose** — the business "why" behind a definition; canon drafts it, you refine it.
- **Guardrails & contracts** — mandatory filters, required dimensions, final-vs-provisional rules. Added when a number needs protecting.
- **Non-additive metrics** — ratios, averages, distinct counts, balances. Declared as composable definitions so they stay correct at any grain.
- **More sources** — dbt / LookML / Metabase / Notion / docs, layered on as context evidence.

The design principle throughout: **canon proposes, you approve.** It never silently edits your context — every change is a reviewable diff.

---

## Install

> canon ships as one distributable (CLI + local daemon). Local embeddings are an optional add-on.

**npm** (dev machines):
```bash
npm install -g @canon/cli
```

**Homebrew** (macOS / Linux):
```bash
brew install canon
```

**Docker** (CI, headless, air-gapped):
```bash
docker pull canon:latest
```

An **offline / air-gapped install** path (Docker image or vendored tarball, no outbound calls during install) is available for restricted environments.

Verify:
```bash
canon --version
```

---

## Quickstart — your first answer in minutes

The fastest path uses **SQLite**: a local file, no server, no credentials, no network.

```bash
canon setup
```

The wizard walks you through:
1. **Name** your project.
2. **Connect** a source — point at a `.db` SQLite file (zero credentials) or a Postgres connection.
3. **Configure an LLM** — optional, and skippable. The core works without one.
4. **Bootstrap** — canon introspects the schema and drafts your semantics.
5. **First answer** — the wizard runs a real query against your data and shows the result, plus how fresh it is and which definition it used.

You now have a working context layer committed to your repo. Ask your own questions:
```bash
canon query --metrics revenue --dimensions order_date
canon knowledge search "active customer"
```

Review what canon drafted when you're ready — it's all an ordinary git diff:
```bash
canon review
canon status        # always tells you the best next step
```

---

## Connect your agent (MCP)

canon exposes its capabilities to agent clients through a **local, on-demand MCP server** — no always-on hosted service. Verified with **Claude Code, Cursor, and Codex**.

**1. Start the daemon** (binds locally; reads your committed context):
```bash
canon mcp start
canon mcp status
```

**2. Register canon in your client's MCP config.** A standard MCP server entry, for example:
```json
{
  "mcpServers": {
    "canon": {
      "command": "canon",
      "args": ["mcp", "start"]
    }
  }
}
```
(See the per-client docs for the exact config location — Claude Code, Cursor, and Codex each load standard MCP configuration.)

**3. Your agent now has these tools:**

| Tool | What the agent does with it |
| --- | --- |
| `list_metrics` | discover what it can ask for |
| `describe_metric` | grain, dimensions, owning source, freshness |
| `resolve_metric` | check a name resolves; surface ambiguity instead of guessing |
| `compile_query` | get the SQL + metadata without running it |
| `query` | the main path — answer a question, with caveats attached |
| `run_sql` | read-only SQL escape hatch |
| `search_context` | find knowledge and definitions by text |
| `read_knowledge_page` | read a full knowledge page to relay its explanation |
| `propose_change` | stage a reviewable context change (never writes to your warehouse) |

Every answer comes back with the **metadata band** — resolved definition, guardrails fired, freshness, final/provisional — so the agent can caveat honestly. On ambiguity or a blocked guardrail, the tool returns the candidates or the rationale, and the agent **asks** rather than fabricates.

---

## What you can rely on

- **Read-only.** canon never mutates your warehouse. It reads, it never writes back.
- **Propose-only.** It never silently edits your context — every change is a reviewable diff anchored to evidence.
- **Refuse-and-ask.** Ambiguous or unsafe? It returns a structured reason, not a guess.
- **No LLM in the answer path.** Queries compile deterministically — the same question always produces the same SQL. An LLM only helps *draft* context, never *compute* an answer.
- **Local-first & air-gapped-capable.** Run entirely on your machine with a local model and local embeddings; nothing has to leave your network.
- **Measurable.** A local event log tracks accuracy, freshness, and answer quality — so "trustworthy" is something you can check, not just claim.

---

## Where to go next

- **Concepts** — the three layers and the split rule (the one mental model worth learning first).
- **Canonical bindings** — resolving "which definition wins."
- **Guardrails & contracts** — turning documented caveats into enforced rules.
- **Connectors** — adding dbt, LookML, BI tools, and docs as context sources.
- **Air-gapped operation** — running fully offline with local models.

canon is local-first, git-native, and read-only by design. Start with a SQLite file and one question; grow into the full context layer as your needs do.
