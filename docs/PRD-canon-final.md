# PRD — Context Layer for Data Agents

**Status:** Draft for review
**Owner:** _TBD_
**Last updated:** 2026-06-13

---

## 1. Summary

We are building an open, file-based **context layer** that sits between a company's data stack and the AI agents that query it. It turns warehouse metadata, BI definitions, modeling code, query history, and team docs into three reviewable surfaces — an **executable semantic layer** (YAML), a **searchable knowledge base** (Markdown), and an **authoritative contract layer** (YAML) — and serves them to agents at runtime via MCP and a CLI. Database access is always read-only; all context is versioned in git and reviewed like code.

The product has two connected sides:
1. **Build & maintain** context by ingesting source systems and reconciling new evidence with accepted definitions.
2. **Serve agents** at runtime: search context, return approved metrics, compile them into dialect-correct SQL.

---

## 2. Problem

A database connection alone does not make an agent a competent analyst. Given schema access, an agent still has to guess which table is canonical, which join is safe, which rows are test accounts, and what the business actually means by a metric. Plausible SQL becomes wrong SQL quickly.

Adjacent tools each leave a gap:
- **Company-brain / search tools** index docs and chats but have no join graph, no canonical metrics, and no path to safe SQL.
- **Traditional semantic layers** compile correct SQL but are hand-maintained, do not learn from the surrounding warehouse/BI/query-history, and separate the *why* (business context) from the *what* (definitions).

We need a single surface that combines business context with executable definitions and stays current as the data stack changes.

---

## 3. Goals & Non-Goals

### Goals
- Give agents one searchable, executable context surface instead of three disconnected ones.
- Keep all context as reviewable, diffable, git-versioned files (YAML + Markdown).
- Auto-maintain context by ingesting the data stack and reconciling evidence with accepted files.
- Compile approved metrics, joins, filters, dimensions, and segments into safe, dialect-correct SQL with correct fanout handling.
- Enforce read-only database access by design.
- Expose all capabilities to agents through both CLI and MCP.
- Support an agent edit loop: propose context changes as reviewable diffs.
- **Learn from answer correctness, not just source drift** — close the loop so corrected/verified queries feed back into context.
- **Govern context org-wide** — consistent semantics, access control, and freshness signals across users, not just per-developer files.

### Non-Goals
- Not a hosted service in v1 (local-first; daemon runs on demand).
- Not a replacement for an existing serving layer — we *ingest* dbt/MetricFlow/LookML rather than force migration.
- Not a write-path to the warehouse. We never mutate source data.
- Not a BI/visualization tool.
- **Not a transformation or orchestration engine.** `canon` does not own scheduling, DAGs, or the write-path. It *gates* pipelines (validation) and *feeds* them (SQL/model emit), but dbt/Airflow/Dagster keep ownership of execution. Competing with them would dissolve the read-only/governance positioning that the trust story depends on.

---

## 4. Target Users & Personas

- **Data/analytics engineer** — owns the context layer, reviews diffs, curates canonical definitions.
- **Analyst** — writes knowledge pages (caveats, policies, definitions), trusts compiled answers.
- **Agent operator / developer** — wires an agent client (Claude Code, Cursor, Codex) to the context layer via MCP.
- **The agent itself** — a first-class consumer that searches, compiles, queries, and proposes context updates.

---

## 5. Product Architecture

### 5.1 The three surfaces (committed to git)

The context layer has three committed surfaces along two axes: *executable vs. interpretable* (the original two pillars) and a third *authoritative / governing* axis that cross-cuts both.

| Surface | Format | Nature | Answers |
| --- | --- | --- | --- |
| Semantic sources | `semantics/**/*.yaml` | structured, executable, auto-maintained | "How do I query this safely?" |
| Knowledge pages | `knowledge/**/*.md` | free-form, searchable, auto-maintained | "What does this mean to the business?" |
| Contracts | `contracts/**/*.yaml` | authoritative, enforceable, human-owned | "Which definition is canonical, and what must the answer obey?" |

**Split rule:** if a fact changes how the SQL runs → semantic layer (YAML). If a human needs it to trust the answer → knowledge (Markdown). If it governs *which* definition is authoritative or *what an answer must satisfy* across sources → contracts (see FR-13).

The contract surface holds what belongs to no single per-source file: canonical metric→source bindings, enforceable guardrails, assertions, finality/coalescing rules, and access/governance policy. The semantic layer *executes* these; the contract surface *declares* them.

**Enforced caveats (not just documented ones).** Many knowledge caveats describe a correctness risk that must be *prevented*, not merely read — e.g. "`amount` includes refunds unless filtered". A knowledge page that documents such a rule without enforcing it documents a bug. Such caveats become contracts (guardrails): mandatory default filters, required-dimension constraints, or hard warnings the compiler emits on violation. The knowledge page explains *why*; the contract makes the SQL *obey*.

**Freshness as a first-class signal.** Trust is not binary. Every surface carries last-validated-against-source metadata; serving surfaces a staleness signal to the agent at query time (e.g. "this definition has not been validated against the warehouse in 90 days") instead of only pruning dead references.

A semantic source describes a table the way an agent can reason about it: row grain, typed columns, named measures, valid joins, filters, segments. Knowledge pages cross-reference semantic sources (`sl_refs`) and other pages (`refs` / `[[links]]`), forming a navigable graph. References are validated on write and pruned during ingest when targets disappear.

### 5.2 Project layout on disk

```text
my-project/
├── canon.yaml                  # project config + connections
├── semantics/
│   └── <connection-id>/*.yaml
├── knowledge/
│   ├── global/*.md             # shared business context
│   └── user/<user-id>/*.md     # user-scoped notes
├── contracts/
│   ├── metrics/*.yaml          # canonical metric→source bindings
│   ├── guardrails/*.yaml       # enforced filters, finality, access policy
│   └── assertions/*.yaml       # query→expected-result checks
├── raw-sources/<connection-id>/   # ingest artifacts & reports
└── .canon/                     # local runtime state + secrets, git-ignored
```
Commit config, semantic layer, knowledge, and contracts. Keep local state out of the repo.

### 5.3 Ingestion flow
`Source connectors → Context builder → Reconciliation → Validation → outputs (knowledge + semantic layer)`
1. **Source connectors** read each system in its native shape.
2. **Context builder** turns evidence into proposed context updates.
3. **Reconciliation** merges new evidence with existing accepted context.
4. **Validation** checks references and semantics before agents rely on them.

Scan snapshots and a per-run event log are kept locally so every committed change is traceable to its evidence (audit trail).

### 5.4 Serving flow
`Agent (plain language) → context layer (MCP/CLI) → search knowledge + semantic layer → return approved metrics → compile to SQL → read-only DB executes`
The context layer is the only component touching both the context files and the warehouse; every DB connection is read-only.

### 5.5 Protocol-agnostic core capability layer

The storage surfaces (semantic layer, knowledge) and the access surfaces (MCP, ACP, CLI) are **orthogonal**: the pillars define *what context is*; the protocols define *how it is reached*. To keep them from leaking into each other, `canon` defines its capabilities **once** in a protocol-neutral core, and exposes each protocol as a thin adapter over it.

```text
Storage          Core capabilities             Surfaces (thin adapters)
─────────        ──────────────────            ────────────────────────
semantics/       search   — find context        MCP server
knowledge/         →  resolve  — name → definition →  ACP adapter
contracts/       compile  — semantic query→SQL    CLI
                 query    — run read-only SQL     headless/CI runner
                 propose  — patch + validate
```

Design rules:
- Capabilities (`search`, `resolve`, `compile`, `query`, `propose`) live in the core and are the *only* place business logic exists.
- Adapters do protocol translation **only** — request/response mapping, auth, transport. No compilation, ranking, or validation logic in an adapter.
- The data model never changes shape to suit a protocol; adding ACP next to MCP is a new adapter, not a storage or core change.
- Headless/pipeline mode (§5.6) is itself just another adapter over the same core, which is why determinism holds across surfaces.

This is the seam that makes multi-protocol support cheap and keeps the pillars stable regardless of how many access surfaces we add.

### 5.6 Operating modes

`canon` runs in two distinct modes that share the same context files and the same deterministic compiler, but differ in who drives and whether an LLM is in the loop.

| | **Interactive / agent mode** | **Headless / pipeline mode** |
| --- | --- | --- |
| Driver | A human or an agent client (Claude Code, Cursor, Codex) via MCP/CLI | A scheduler or CI runner (cron, GitHub Actions, Airflow task) |
| LLM in loop | Yes — for search, reconciliation drafting, knowledge authoring | No (or optional, off the critical path) — must be deterministic |
| Output | Answers, proposed diffs, trust-scored results | Exit codes, emitted SQL/models, auto-PRs, validation reports |
| Primary value | Ad-hoc analysis and context repair | Repeatable governance and contract enforcement |

**Why this works:** the compiler is deterministic (YAML → SQL, no LLM in the hot path), so the compile/query path is already pipeline-safe — reproducible, no model nondeterminism. The agentic trust-score and edit loop simply do not apply in headless mode; it is a different operating mode, not a feature toggle.

**Three pipeline-fit roles** (all preserve read-only and the non-orchestration guardrail):
1. **Scheduled ingest** — `canon ingest` as a cron/CI job that opens reconciled diffs as auto-PRs against git. A context-maintenance pipeline.
2. **Semantic validation as a CI gate** — a dbt/model change runs against `canon`'s assertions and guardrails: "does this change break a known canonical definition?" `canon` becomes a **data-contract test** inside the existing pipeline. *(Strongest near-term role; see FR-4.)*
3. **Metric materialization by SQL emit** — `canon` compiles approved measures into SQL/models that a transformation step materializes. `canon` emits; the pipeline executes and owns the write-path.

**The guardrail that holds it together:** `canon` stays the *definition and governance authority*. It may gate pipelines and feed them, but it owns neither orchestration nor the write-path. This also inverts the dbt relationship from "we ingest dbt" to "we feed *and* validate dbt" — bidirectional, not just one-directional ingest.

**What it costs:** headless mode requires a non-interactive, LLM-free execution path with clear CI exit codes, and raises the bar on E5 — the compiler must be source-of-truth-grade. This touches FR-1 (a headless entry point) and FR-4 (assertions usable as a CI gate) without adding a new capability area.

---

## 6. Functional Requirements

Grouped by capability area; each area maps to a candidate epic (§9).

### FR-1 Project & Configuration
- Initialize a project (`setup` wizard) that resumes from saved progress if interrupted.
- `canon.yaml` defines project config, connections, and LLM configuration.
- Distinguish committed files (config, semantic layer, knowledge, contracts) from git-ignored local state/secrets.
- Interactive entry point: bare command opens a wizard outside a project, a resume/connect/status menu inside one.
- **Distribution & installation (v1):** ship the CLI and local daemon through three channels — npm global package (scoped, since the bare `canon` name is taken), Homebrew formula, and a Docker image for the daemon/headless runner (also the basis for CI usage). One documented install command per channel.
- **Offline install path:** an air-gapped install option (no outbound calls during install) to match the offline/air-gapped runtime mode (FR-8).
- **Headless entry point:** a non-interactive invocation for CI/pipeline use with explicit exit codes (no wizard, no prompts), per §5.6.
- Version/upgrade story: `canon` reports its version; daemon and CLI versions must be compatible (checked at start).

### FR-2 Source Connectors
- **Primary sources (databases, read-only):** PostgreSQL, Snowflake, BigQuery, SQLite — extract schemas, columns, keys, row counts, query history.
- **Context sources:** BI tools (Metabase, Looker — dashboards, questions, explores, usage, trusted examples); modeling code (dbt, LookML, MetricFlow — metrics, dimensions, models, joins, entities); docs/notes (Notion, arbitrary text — policies, caveats, definitions).
- Connection lifecycle management (add, test, list, remove); connection test must pass before building context.

**Connector contract (what makes "any vendor" real).** Extensibility is a *contract*, not just an interface. The sources are not homogeneous, so the abstraction is capability-based, not vendor-based.
- **Three connector classes**, each with a narrow capability set rather than one leaky universal interface:
  - *Primary / queryable* (databases): `introspect_schema`, `read_query_history`, `run_read_only_sql`. Feeds semantic layer + compiler.
  - *Definition* (dbt, LookML, MetricFlow): `extract_definitions` (measures, joins, entities). Feeds the semantic layer only — not the query path.
  - *Evidence* (Metabase, Looker, Notion, text): `extract_evidence` (dashboards, usage, prose). Feeds knowledge + reconciliation signal.
- **Capability declaration, not identity:** the core asks a connector *what it can do*, never *who it is*. A new vendor implements the relevant capabilities; the core does not change. No vendor name appears in core logic.
- **Normalized evidence schema (the real lever):** every connector translates its native output into one internal evidence format. The context builder and reconciliation never see Snowflake- or dbt-specific shapes — only normalized evidence. This seam is what stops each new vendor from leaking into the core.
- **SQL dialect is a separate abstraction:** the compiler's dialect adapter is decoupled from the source connector. "Add a database" (ingest) and "support its dialect" (compile) are independent extension points, so neither blocks the other.
- **Out-of-tree plugins (v1 requirement):** third parties can ship a connector without forking the core — a declared plugin interface, a discovery mechanism, and a versioned connector-contract. "Pluggable" must be true externally, not just inside the monorepo. *(This is a deliberate differentiator: an open, versioned connector SDK rather than vendor-by-vendor implementations.)*
- **Conformance harness:** a connector test kit asserts a candidate connector satisfies its class contract and emits valid normalized evidence, so new/third-party connectors can be certified without manual review.

**Schema acquisition ladder.** `introspect_schema` is a declared capability, not a requirement. When a source cannot provide it — blocked catalog rights, an exotic store, or a not-yet-supported vendor — `canon` descends a prioritized fallback chain rather than failing hard. All paths produce the same normalized evidence schema; the tier is recorded as provenance.

| Priority | Method | When to use |
| --- | --- | --- |
| 1 | **Live introspection** | Catalog views available (`information_schema`, `pg_catalog`, `SVV_*`, etc.) — standard path |
| 2 | **Modeling code as schema source** | dbt/LookML/MetricFlow present; often *better* than raw introspection because already curated |
| 3 | **Query-history inference** | Observed queries reveal tables, columns, and joins in use — aligns with query-history-first bootstrapping |
| 4 | **Declarative schema import** | User supplies a DDL dump, `information_schema` export, or schema YAML; `canon` ingests it as evidence |
| 5 | **Sample-based inference** | Read-only `SELECT … LIMIT n` against known tables to derive columns/types when catalog views are absent |
| 6 | **Hand-authored `semantics/*.yaml`** | Last resort: user authors the semantic source directly; `canon` validates it against the live source via probe query |

- `canon` reports which tier it used per source, so the user always knows how the schema was acquired.
- Partial capability is never silent: if only some tables are introspectable, `canon` documents the gap and asks whether to proceed or supplement via a lower tier.

- **Published v1 compatibility matrix (scopes E3).** Each connector pins explicit supported versions and degrades gracefully (clear "unsupported version" error, never silent partial ingest). Proposed v1 targets, to be confirmed during the E3 spec against then-current releases:

  | Source | v1 supported (proposed) | Notes |
  | --- | --- | --- |
  | PostgreSQL | 13+ | via `information_schema` + `pg_stat_statements` for query history |
  | Snowflake | current GA | `INFORMATION_SCHEMA` + `QUERY_HISTORY` |
  | BigQuery | current GA | `INFORMATION_SCHEMA` + jobs/query history |
  | SQLite | 3.35+ | local/dev |
  | dbt | dbt Core 1.6+ (manifest schema vN) | parse `manifest.json`; Cloud Semantic Layer out of scope for v1 |
  | MetricFlow | as bundled with the supported dbt range | tracks dbt's semantic spec, not a separate pin |
  | LookML / Looker | API 4.0 | LookML via Looker API; raw repo parsing later |
  | Metabase | 0.48+ | REST API for questions/dashboards |
  | Notion | current API version | API-version header pinned |

- Connectors are versioned independently; the matrix is published per release so users know what works before installing.

### FR-3 Ingestion & Auto-Maintenance
- Fast initial schema ingest during setup to bootstrap context.
- Full ingest that reconciles new evidence against accepted files and emits version-controlled diffs.
- **Provenance tiers:** every fact carries its origin (e.g. board-approved > human-curated > inferred-from-evidence). Higher tiers win during reconciliation; ingest never overwrites a higher tier with a lower one.
- **Propose-only by default, with confidence:** ingest proposes diffs with a confidence score rather than silently auto-editing accepted files; an explicit policy/threshold governs any auto-apply.
- **Freeze annotations:** human-owned facts can be marked frozen so reconciliation will flag conflicting evidence but never edit them.
- Reconciliation must surface and flag contradictions across sources rather than silently overwrite.
- Re-runnable ingest that refreshes from databases, BI tools, query history, and documents.
- **Schema validation probe:** whenever schema is acquired via tiers 4–6 of the acquisition ladder (declarative import, sample inference, or hand-authored), `canon` issues a read-only probe query against the live source to verify the declared schema matches reality before the evidence is committed. Mismatch → validation error with a diff of declared vs. observed, never silent acceptance.
- Persist scan snapshots + event log per run for traceability.

### FR-4 Semantic Layer (definition + compiler)
- Author semantic sources as YAML: `name`, `table`, `grain`, typed `columns`, `measures` (with `expr` + optional `filter`), `joins` (`to`, `on`, `relationship`), dimensions, filters, segments.
- Compiler walks the reviewed join graph, handles fanout safely, and transpiles to the target SQL dialect.
- Compile a short semantic query (selected measures/dimensions/filters) into dialect-correct SQL.
- **Additivity-aware compilation:** measures carry additivity flags (additive / semi-additive / non-additive); the compiler refuses or warns on unsafe aggregations rather than silently producing wrong numbers. (P0 compiles additive measures only — `sum`/`count`/`min`/`max`; non-additive handling is P1 per §9.1 / SPEC E5.)
- **Enforce contracts at compile time:** apply the guardrails, finality/coalescing rules, and access policy *declared* in the contract surface (FR-13) — mandatory filters, final-only constraints, masking — and run query assertions against compiler output. The semantic layer executes; the contract surface declares.
- **Propagate result attributes:** attach a provisional/final flag (from finality rules) and guardrail outcomes onto every result for the trust score (FR-12).
- **Freshness metadata:** each source records last-validated-against-source timestamp, exposed to serving as a staleness signal.
- Validate semantic sources (reference integrity, types, grain, contract consistency) before serving.

### FR-5 Knowledge (authoring + retrieval)
- Author knowledge pages as Markdown with frontmatter: `summary`, `tags`, `sl_refs`, `refs`, `usage_mode`.
- Hybrid search/ranking over knowledge pages; traverse the `sl_refs` / `refs` / `[[links]]` graph without re-searching.
- Auto-author pages from ingest evidence; validate references on write; prune stale `sl_refs` during ingest.
- Support global and user-scoped pages.
- **Scope conflict rule (v1, decided):** `knowledge/user/<id>` is **strictly additive**. A user page can add context but can never override or shadow a global definition. On a name/topic collision the global page is authoritative; the user page is surfaced as a personal annotation attached to it, never as a replacement. This keeps a single shared source of truth and matches the governance posture; richer per-team overrides are deferred past v1.

### FR-6 Serving — CLI & MCP
- CLI commands covering setup, connections, ingest, semantics ops (`sl` — resolve/compile), compiled query (`query`), raw SQL (`sql`, read-only), knowledge ops, status, MCP daemon control, admin, completion.
- MCP server exposing the same capabilities to agent clients: search context, find semantic entities, compile semantic queries, run read-only SQL, propose updates.
- **Surface freshness + guardrail context at query time:** when returning a definition or compiled SQL, attach staleness signals and any enforced guardrails so the agent can caveat its answer.
- Local MCP daemon runs on demand (no always-on hosted service); `status` instructs when to start it.
- Verified agent-client integration: Claude Code, Cursor, Codex.

### FR-7 Agent Edit Loop & Review
- Agents can patch semantic YAML and knowledge Markdown, validate, and produce reviewable diffs.
- Changes flow through the standard code-review workflow: branch → review YAML/Markdown diffs → merge → agents read updated source of truth.
- Repair workflow: fix context through reviewable diffs anchored to evidence.

### FR-8 Runtime LLM / Embeddings Configuration
- Bring-your-own LLM keys; configurable provider/model.
- **Local & self-hosted open-source LLMs as a first-class target.** Support an OpenAI-compatible `base_url` so any local runtime — Ollama, vLLM, LM Studio, llama.cpp, text-generation-inference — docks in without engine-specific code.
- **Fully offline / air-gapped mode:** local generation LLM + local embeddings together must support a configuration where no warehouse content or context ever leaves the machine/network. This is a privacy differentiator, not just a convenience.
- Configurable per task (e.g. cheaper/local model for ingest drafting, stronger model for reconciliation) rather than one global model.
- Document a tested baseline of local models so self-hosters know what actually works for compilation/reconciliation quality.
- Optional local embeddings feature (installable runtime) for semantic search.

### FR-9 Answer Feedback Loop (self-improving)
- Capture outcomes of served answers: analyst corrections to compiled SQL, explicit correct/incorrect marks, and edits applied downstream.
- Feed these outcomes back into reconciliation as first-class evidence, so the system learns from real answer errors, not only schema drift.
- A repeatedly-corrected definition raises a review signal (or proposes a guardrail/assertion) rather than silently persisting the error.
- Keep the loop auditable: every learned change traces to the outcome that triggered it.

### FR-10 Governance & Org-Wide Consistency
- **Access control:** row-level security and column/PII masking policies (declared in the contract surface, FR-13) applied at compile/serve time, so the same definition yields appropriately scoped results per user/role.
- **Shared semantics:** mechanism to guarantee all users query the same canonical definitions (e.g. a published/locked context version), not divergent local copies.
- **Policy as reviewable config:** access and masking rules live as versioned files (contract surface) reviewed like the rest of the context.
- Note: this is a major capability for org-wide deployment and likely spans v1→v2.

### FR-11 Operational Safety & Query Cost Control
- **Dry-run + cost estimate before execution:** estimate scanned bytes / rows and surface it to the agent before running, so exploratory querying does not silently burn warehouse budget.
- **Hard limits:** configurable byte/row/time ceilings per query and per session; queries exceeding them are blocked or require confirmation.
- **Semantic result cache:** identical compiled queries return cached results within a configurable TTL, cutting cost and latency for repeated agent calls.
- **Per-connection budgets:** optional spend/scan budget with alerting when approached.
- Rationale: read-only protects the *data*; this protects *operations and cost*.

### FR-12 Answer Trust Score
- **Single composite score** attached to every served answer, derived from existing signals: provenance tier (FR-3), freshness (FR-4), assertion coverage (FR-4), guardrails fired (FR-4), and feedback-loop history (FR-9).
- The score and its contributing factors are returned alongside the compiled SQL/result so the agent can caveat truthfully (e.g. "confidence 0.6 — definition unvalidated for 90 days, no assertion, one guardrail fired").
- **Threshold-driven behavior:** below a configurable threshold the system can refuse-and-explain rather than answer, or escalate to a human metric owner instead of guessing.
- Score weighting is configurable and the breakdown is always inspectable (no opaque number).

### FR-13 Contract Surface (canonical bindings, guardrails, finality, policy)
The third committed surface (`contracts/`). It *declares* what is authoritative and what answers must obey; the semantic layer compiler (FR-4) *enforces* it. It holds the cross-cutting facts that belong to no single per-source YAML.
- **Canonical metric bindings:** each logical metric name resolves to exactly one owning measure/source, with provenance tier; alternative definitions are marked deprecated/aliased. Genuine ambiguity → refuse-and-ask, never guess.
- **Enforceable guardrails:** mandatory default filters, required-dimension constraints, and final-only/context restrictions — declared here, applied by the compiler.
- **Finality & multi-realization:** for a metric with multiple physical sources along a finality axis (batch authoritative through T-1 + intraday real-time estimate), declare a per-source finality watermark and a coalescing rule (which source serves which time window); a provisional/final flag is propagated onto every result.
- **Assertions:** trusted query→expected-result checks, runnable as a CI gate (headless mode) and as a regression oracle in compilation.
- **Access & masking policy:** RLS and column/PII rules (the FR-10 governance policy lives here).
- **Drift detection:** critical knowledge claims promoted to assertions; a measure-definition fingerprint flags bound knowledge pages for review when the `expr` changes.
- **Reviewable & validated:** contract files are versioned and reviewed like everything else; validation checks that bindings, guardrails, and assertions reference live semantic entities.

### FR-14 Instrumentation & Metrics
The product must be able to *prove* the success metrics (§8), not just aspire to them. This is the measurement substrate behind the feedback loop (FR-9) and the privacy-conscious telemetry NFR.
- **Local event log:** every served answer records a structured event — query, resolved metric/source, compiled SQL hash, trust score, guardrails fired, provisional/final mix, latency, bytes scanned, cache hit. Local by default; nothing leaves the machine unless telemetry is opted in.
- **Accuracy harness:** a repeatable benchmark mode runs a labeled question set through compilation and compares against assertions/known-correct results to compute query-accuracy — the mechanism behind the ">90% accuracy" claim. Re-runnable in CI so accuracy is tracked over time, not asserted once.
- **Outcome capture:** analyst correct/incorrect marks and corrections (the FR-9 signal) are logged as the ground-truth feed for accuracy and correction-recurrence metrics.
- **Derived metrics:** time-to-first-correct-answer, cache hit rate, blocked-over-limit count, freshness lag, contradiction-detection rate — all computable from the local event log without external services.
- **Opt-in aggregate telemetry:** privacy-conscious, disclosed, off by default; aggregate/anonymized only; never query results or warehouse content. A clear opt-out and a documented schema of what is sent.
- **Inspectable:** users can view their own local metrics (e.g. `canon status`/report) without enabling any telemetry.

---

## 7. Non-Functional Requirements

- **Safety:** all database connections strictly read-only; never write to source data.
- **Reviewability:** every committed change diffable and traceable to source evidence.
- **Local-first:** runs on the user's machine; secrets and runtime state stay local and git-ignored.
- **Interoperability:** ingest existing dbt/MetricFlow/LookML/BI definitions without forcing migration.
- **Resumability:** long-running setup/ingest resume after interruption.
- **Extensibility:** a versioned, capability-based connector contract with a normalized evidence schema and an out-of-tree plugin SDK lets third parties add sources without forking the core; the SQL dialect is a separate extension point.
- **Licensing:** open source (Apache-2.0 target).
- **Privacy:** any telemetry must be privacy-conscious and disclosed.
- **Data residency:** an offline/air-gapped configuration (local LLM + local embeddings) must keep all warehouse content and context on-machine/in-network.

---

## 8. Success Metrics (proposals)

All metrics below are measured by the instrumentation substrate (FR-14) from the local event log and accuracy harness, so they are provable and trackable over time rather than aspirational.

- Agent query accuracy on a benchmark set (target: high-90s%) vs. schema-only baseline.
- Share of analytics requests answerable without human SQL authoring.
- Time-to-first-correct-answer after `setup`.
- Context freshness: lag between a warehouse change and a reconciled diff.
- Contradiction detection rate during reconciliation.
- Correction recurrence: how often the same definition is re-corrected after a feedback-loop update (should trend down).
- Guardrail/assertion coverage of high-risk measures (non-additive, fanout-prone).
- Share of served answers carrying an accurate freshness/guardrail caveat.
- Warehouse cost/bytes scanned per answer; cache hit rate; blocked-over-limit query count.
- Trust-score calibration: correlation between low scores and actual answer errors (low score should predict wrongness).

---

## 9. Proposed Epic Breakdown

| Epic | Scope | Primary FRs |
| --- | --- | --- |
| E1 — Project foundation & config | `canon.yaml`, setup wizard, project layout, resumability, distribution/install (npm/Homebrew/Docker) | FR-1 |
| E2 — Primary source connectors | Read-only DB connectors + schema/query-history extraction; connector contract, normalized evidence schema, plugin SDK + conformance harness (shared substrate for E3) | FR-2 |
| E3 — Context source connectors | Definition + evidence connectors (BI, modeling code, docs) on the E2 connector contract + published v1 compatibility matrix | FR-2 |
| E4 — Ingestion & reconciliation engine | Builder, reconciliation, contradiction flagging, snapshots | FR-3 |
| E5 — Semantic layer & compiler | YAML model, join graph, fanout, dialect transpile, validation | FR-4 |
| E6 — Knowledge & retrieval | Authoring, frontmatter graph, hybrid search, ref pruning | FR-5 |
| E7 — CLI surface | All `canon <command>` subcommands | FR-6 |
| E8 — MCP server & agent clients | MCP tools, daemon lifecycle, client integrations | FR-6 |
| E9 — Agent edit & review loop | Patch/validate/diff/review workflow | FR-7 |
| E10 — LLM & embeddings runtime | Provider config, local/self-hosted OSS LLMs, offline mode, local embeddings | FR-8 |
| E11 — Answer feedback loop | Outcome capture, feedback into reconciliation, learned-change audit | FR-9 |
| E12 — Governance & org consistency | RLS, PII masking, shared/locked context versions, policy-as-config | FR-10 |
| E13 — Operational safety & cost control | Dry-run/cost estimate, hard limits, result cache, budgets | FR-11 |
| E14 — Answer trust score | Composite score, threshold behavior, inspectable breakdown | FR-12 |
| E15 — Contract surface | Canonical bindings, guardrails, finality, assertions, policy, drift detection | FR-13 |
| E16 — Instrumentation & metrics | Event log, accuracy harness, derived metrics, opt-in telemetry | FR-14 |

Each epic decomposes into specs along its FRs. **E5, E6, and E15 are the highest-complexity and should be spec'd first** since serving depends on them — and E5 enforces what E15 declares, so the two are tightly coupled and should be designed together (the contract schema and the compiler's enforcement hooks are two halves of one interface). E11 depends on E4 (it extends reconciliation); E12 governance policy now lives in the E15 contract surface and may split across v1/v2.

### 9.1 Release sequencing

Sixteen epics do not ship at once. The path below front-loads a working end-to-end loop for a *single* source, then layers on auto-maintenance, then the trust/ops differentiators.

**Phase 0 — Walking skeleton.** Prove the spine end-to-end for one database.
- E1 (foundation/install), E2 (one primary connector, e.g. PostgreSQL), E5 + E15 (compiler + minimal contract: canonical bindings, basic guardrails), E7 (CLI), E8 (MCP serving).
- Exit: an agent asks for a metric → `canon` resolves the canonical binding → compiles dialect-correct read-only SQL → returns a result. No LLM required in this path.

**Phase 1 — v1 core (the product thesis).** Auto-built context across both pillars and multiple sources.
- E4 (ingestion + reconciliation), E6 (knowledge + retrieval), E10 (LLM config incl. local/offline), E3 (definition + evidence connectors: dbt, BI, docs), fuller E15 (finality, assertions, drift detection), minimal E16 (event log so accuracy is measurable from day one).
- Exit: `canon ingest` bootstraps context from a real stack; agents get both executable definitions and business meaning; accuracy is tracked.

**Phase 2 — Trust & operations (hardening for org use).** The differentiators.
- E13 (cost control), E14 (trust score) + full E16 (accuracy harness, telemetry), E11 (feedback loop), E9 (agent edit/review loop), E12 (governance: RLS/PII, shared/locked versions).
- Exit: answers carry trust scores, costs are bounded, the system learns from corrections, and it is safe for multi-user org deployment.

**Deferred past v1.** ACP adapter (after MCP is proven), out-of-tree third-party connector SDK GA (contract must stabilize first — see §10), richer per-team knowledge overrides.

Critical-path dependency: nothing in Phase 1 or 2 is safe to spec before the Phase 0 E5+E15 interface is fixed, because both phases serve through it.

**Detailed specs.** Phase 0 is specified in four companion documents: E1 (foundation/config/distribution), E2 (primary source connector), E5+E15 (semantic layer & compiler + contract surface), and E7+E8 (CLI + MCP serving). They share one semantic-query format, one `QueryResult` object, and one canonical error registry (SPEC E7+E8 §6.1).

---

## 10. Risks & Open Questions

- **Reconciliation policy tuning:** provenance tiers and confidence thresholds reduce the risk, but the auto-apply threshold and tier ordering still need calibration against real projects.
- **Compiler fanout edge cases:** many-to-many joins and additive vs. non-additive measures require explicit semantics (now backed by additivity flags + assertions).
- **Dialect coverage — P0 decided.** PostgreSQL ships first (SPEC E2/E5); the dialect adapter is decoupled from the connector so further dialects (Snowflake, BigQuery) are independent extension points. Open: the order after Postgres and a transpilation test strategy per dialect.
- **Search quality without local embeddings:** fallback ranking when embeddings are disabled.
- **Multi-user knowledge scoping — decided (FR-5).** v1 rule: `knowledge/user` is strictly additive; global is authoritative on collision, user pages surface as personal annotations and never override. Richer per-team overrides deferred past v1.
- **Secret handling — partly decided.** SPEC E1 decides indirection: `canon.yaml` holds only references (`env:` / `keyring:` / `file:.canon/…`), never literal secrets, validated on load. Open: the on-disk secret format under `.canon/` and rotation.
- **Feedback-loop signal quality:** how to distinguish a genuine definition error from a one-off analyst override without over-fitting to noise.
- **Governance vs. local-first:** RLS/masking and shared-version enforcement likely require a trust boundary the current per-developer runtime does not have — where does that boundary live (local, shared service, git policy)?
- **Guardrail authoring burden:** who promotes a knowledge caveat to a compile-time guardrail, and how is that kept from becoming a bottleneck?
- **Connector-contract stability.** SPEC E2 defines the capability-based contract and normalized evidence schema; the out-of-tree plugin SDK only delivers "any vendor" if that contract is stable enough to commit to early — but it will still be learning from the first few connectors. Open: when to freeze the v1 contract, and how third-party connectors are versioned/certified against it.
- **Description ↔ implementation drift — addressed (FR-13 drift detection).** Resolution: knowledge references measures by name and renders the live `expr` rather than restating it; a definition fingerprint flags bound knowledge pages for review when the `expr` changes. Remaining detail: tuning what counts as a review-worthy change.
- **Metric → source canonicality — addressed (FR-13 canonical bindings).** One metric name → one owning measure with provenance tier; alternatives deprecated/aliased; genuine ambiguity triggers refuse-and-ask. Remaining detail: bootstrapping bindings during initial ingest without overwhelming review.
- **Third surface — decided (FR-13 / E15).** Guardrails, governance policy, assertions, canonical metric bindings, finality rules, and drift detection are all cross-cutting and now live in a formalized `contracts/` surface alongside semantics and knowledge. The worked example below drove the decision.

  **Worked example — one metric, two realizations (batch vs. real-time / Lambda problem).** A warehouse is authoritative for `revenue` through business day T-1; an intraday real-time store back-fills today's estimate. Same logical metric, different *finality*. Naive handling produces two bugs: a single UNION'd measure that silently returns a number that changes overnight, or two unrelated metrics (`revenue_final`, `revenue_estimate`) the agent can't reconcile — and "last 7 days incl. today" becomes unanswerable. Correct modeling spans all three surfaces:
  - *Semantic layer (YAML):* both physical sources with their grain, a first-class **finality watermark** ("warehouse authoritative through business day T-1 in TZ X"), and the **coalescing rule** (window ≤ watermark → warehouse; > watermark → real-time store).
  - *Knowledge (Markdown):* what "estimate" means — provisional intraday, finalized overnight, may move ±X%, not for board reporting.
  - *Third surface:* the canonical binding `revenue = {final, realtime}` with the resolution rule, plus the enforced contract ("speed-layer results MUST be labeled provisional"; "context = board reporting → final only").
  - *Result attribute, not internal detail:* a "last 7 days" query returns 6 final + 1 provisional days **with the provisional portion labeled**. This feeds the trust score (FR-12, provisional lowers it) and a guardrail (FR-4, final-only is enforced, not just documented).
  This is the cleanest argument yet *for* the third surface, and it raises a new **E5/FR-4 requirement**: model finality watermarks and a source-coalescing rule, and propagate a provisional/final flag onto every result.

---

## 11. Out of Scope (v1) / Future

- Hosted/cloud serving and managed daemon.
- Write-back or transformation orchestration.
- Native visualization/dashboarding.
- Expanded connector catalog (long-tail systems) beyond the v1 set.
