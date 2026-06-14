# Spec — E7 CLI + E8 MCP Serving Surfaces

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-6; §5.5 core capability layer, §5.6 operating modes; §9.1 Phase 0)
**Related:** SPEC E2 (`query` execution), SPEC E5+E15 (`resolve`/`compile`)
**Last updated:** 2026-06-13

E7 and E8 are specified together because both are **thin adapters over one protocol-agnostic core** (PRD §5.5). The capabilities are defined once in the core; CLI and MCP only translate transport. ACP is the same pattern, deferred. This spec defines the core capability surface and the two P0 adapters.

Phase markers: **[P0]** walking skeleton, **[P1]** v1 core, **[L]** later.

---

## 1. Scope

In scope:
- The **core capability layer**: the single definition of `resolve`, `compile`, `query`, `search`, `propose` and which are P0.
- The **adapter rule**: what an adapter may and may not do.
- **CLI adapter** (E7): command surface mapping to capabilities; interactive + headless modes.
- **MCP adapter** (E8): tool surface for agent clients; daemon lifecycle; verified clients.
- Result/error parity across surfaces.

Out of scope (own specs): the capability *internals* — schema introspection/execution (E2), resolution/compilation/enforcement (E5/E15), knowledge search (E6), edit/propose loop (E9); trust-score computation (E14, whose output we pass through); cost control (E13).

---

## 2. Core capability layer

The core exposes a small, stable, transport-neutral capability set. Every surface calls these; none reimplements them.

```text
Capability                         Phase   Summary
────────────────────────────────  ─────   ───────────────────────────────────────
list_metrics() / describe_metric(name)  P0   discovery: canonical metrics, dims, sources
resolve(name, context)             P0      name → canonical binding | Ambiguous | Unresolved   (E15)
compile(semantic_query)            P0      semantic query → SQL + metadata, no execution        (E5)
query(semantic_query)              P0      compile + execute (read-only) → QueryResult           (E5→E2)
run_sql(sql, connection)           P0      execute a read-only SQL string on a named connection  (E2)
search(text)                       P1      hybrid search over knowledge + semantics              (E6)
propose(patch)                     P1      validate + stage a reviewable diff                    (E9)
```

- `query` is the primary agent path: it resolves, compiles, enforces contracts, executes read-only, and returns a **`QueryResult`** (§2.2) so the caller can present a trustworthy answer.
- `compile` (no execute) exists for inspection/CI.
- **Capability→implementation mapping:** core `run_sql` calls the connector capability `run_read_only_sql` (E2); `connection` is required and defaults to `project.default_connection` (E1) when omitted. Core `describe_metric` is the same capability the MCP/CLI surfaces expose under that name (no separate `describe`).
- All capabilities return **structured results and structured errors** (the canonical error registry, §6.1), never prose-only — both adapters depend on this.

### 2.2 `QueryResult` — the combined `query` response [P0]

`query` merges the compiler metadata (E5) and the executed `ResultSet` (E2) into one object. This is the contract the agent answer depends on, so it is pinned here.

```json
{
  "result": { "columns": [{"name":"order_date","type":"date"},{"name":"revenue","type":"decimal"}],
              "rows": [["2025-01-01", 12000.50]],
              "truncated": false,
              "bytes_scanned": 10485760 },
  "compiled": { "sql": "SELECT …", "dialect": "postgres" },
  "metadata": { "resolved": {"metrics": {"revenue": "orders.total_revenue"}},
                "guardrails_fired": [{"id": "revenue-excludes-refunds", "kind": "mandatory_filter"}],
                "finality": {"final_rows": "<=watermark", "provisional_rows": ">watermark"},
                "freshness": [{"source":"orders","last_validated_at":"2026-06-13T00:00:00Z","stale":false}] }
}
```

- `metadata` is exactly the E5 compiler metadata; `result` is exactly the E2 `ResultSet`. No surface adds or renames fields.
- `compile` returns `compiled` + `metadata` (no `result`).

### 2.1 Adapter rule [P0]

An adapter does transport translation **only**: request parsing, auth, serialization, presentation. It contains **no** resolution, compilation, ranking, validation, or canonicality logic. Consequence: any two surfaces, given the same core state and the same logical request, return identical results. Adding ACP later is a new adapter, not a core change.

---

## 3. CLI adapter (E7) [P0]

Single binary `canon`. Subcommands map to capabilities and to lifecycle from other epics.

```text
canon setup                 # E1 wizard (out of scope here)
canon connection add|test|list|remove   # E2 lifecycle
canon ingest                # E4 (P1)
canon sl resolve <name>     # core.resolve
canon sl compile -f q.json  # core.compile  → prints SQL + metadata
canon query   -f q.json     # core.query    → runs read-only, prints result + metadata
canon sql --connection <id> "SELECT …"   # core.run_sql (read-only; defaults to project.default_connection)
canon knowledge search <q>  # core.search (P1)
canon status                # health, daemon state, acquisition tiers, freshness
canon mcp start|stop|status # E8 daemon control
canon completion            # shell completion
```

- **Interactive mode [P0]:** bare `canon` opens a wizard outside a project; a resume/connect/status menu inside one (PRD FR-1).
- **Headless mode [P0]:** every capability command runs non-interactively (no prompts) and returns the structured exit codes from the canonical error registry (§6.1). This is the basis for the §5.6 CI-gate role.
- **Output:** human-readable by default; `--json` emits the raw structured core response for scripting/CI. The `--json` payload is identical to what the MCP adapter returns (parity).

---

## 4. MCP adapter (E8) [P0]

A local MCP server exposing the same capabilities as tools to agent clients.

### 4.1 Tools [P0 unless noted]

```text
MCP tool            → core capability        Purpose
─────────────────     ──────────────────     ─────────────────────────────────────
list_metrics        → list_metrics           agent discovers what it can ask for
describe_metric     → describe_metric        grain, dims, owning source, freshness
resolve_metric      → resolve                check a name resolves; surface ambiguity
compile_query       → compile                get SQL + metadata without running
query               → query                  the main path: answer with metadata
run_sql             → run_sql                read-only SQL escape hatch (takes a `connection` arg)
search_context      → search   [P1]          find knowledge/semantics by text
propose_change      → propose  [P1]          stage a reviewable contract/knowledge diff
```

- Tool results carry the **same metadata block** as the core (guardrails fired, provisional/final, freshness, resolved bindings) so the agent can caveat its answer. On a structured error (`AMBIGUOUS`, `GUARDRAIL_BLOCK`, …) the tool returns the candidates/rationale, enabling the agent to refuse-and-ask rather than fabricate.
- `query` returns the `QueryResult` object (§2.2); `run_sql` requires a `connection` argument (defaults to `project.default_connection`).
- Tools are read-only with respect to source data; `propose_change` (P1) writes only to staged context files, never to the warehouse.

### 4.2 Daemon lifecycle [P0]

- The MCP server runs as an **on-demand local daemon** (no always-on hosted service; PRD FR-6). `canon mcp start/stop/status`; `canon status` instructs the user to start it when needed.
- Binds locally; the daemon reads the project's committed context (`semantics/`, `contracts/`, `knowledge/`) and local state from `.canon/`.
- Version compatibility: daemon and CLI versions are checked on start (PRD FR-1); mismatch is a clear error.

### 4.3 Verified clients [P0]

Documented, tested integration with **Claude Code, Cursor, Codex** via standard MCP configuration. "Verified" = each client can list tools, call `query`, and receive the metadata block, validated by an integration test per client.

---

## 5. ACP & future surfaces [L]

ACP is an additional adapter over the unchanged core, added after MCP is proven (PRD §9.1 deferred). Recorded here so the core capability surface (§2) is designed to accommodate it without change; no ACP work in v1.

---

## 6. Safety & parity [P0]

- **Read-only** is inherited from E2 and holds identically on both surfaces.
- **Parity test:** a conformance test runs the same semantic query through the CLI `--json` path and the MCP `query` tool and asserts byte-identical core payloads — proving the adapter rule (§2.1).
- **Untrusted input:** a semantic query or SQL arriving through an adapter is still subject to all core validation and contract enforcement; an adapter never bypasses the core.

### 6.1 Canonical error registry [P0]

The single source of truth for error codes across all capabilities and surfaces. Codes originate in E2/E5; this table is the registry both adapters and CI map against (headless exit codes, MCP error payloads).

| Code | Origin | Meaning | Exit |
| --- | --- | --- | --- |
| `UNRESOLVED` | E5 | metric name matches no active binding | 2 |
| `AMBIGUOUS` | E5 | name matches >1 active binding; candidates returned | 3 |
| `UNREACHABLE` | E5 | dimension/filter has no join path to the metric source | 4 |
| `AMBIGUOUS_JOIN_PATH` | E5 | >1 valid join path; explicit path required | 5 |
| `UNSUPPORTED_MEASURE` | E5 | non-additive/semi-additive measure requested (P1 feature) | 6 |
| `FANOUT_UNSAFE` | E5 | join would corrupt a non-additive measure (P1) | 7 |
| `GUARDRAIL_BLOCK` | E5/E15 | a `severity: error` guardrail blocked the query; rationale returned | 8 |
| `VALIDATION_FAILED` | E5/E15 | semantic/contract file failed validation | 9 |
| `ASSERTION_FAILED` | E5/E15 | a benchmark/CI assertion diverged from expected | 10 |
| `READ_ONLY_VIOLATION` | E2 | a non-SELECT statement was rejected before execution | 11 |
| `SCHEMA_MISMATCH` | E2 | declared/hand-authored schema diverges from the live source | 12 |
| `CONNECTION_ERROR` | E2 | connection unavailable/failed health | 13 |

`0` = success. Every structured error carries `{code, message, candidates?}`; the `code` maps to the exit value above in headless mode and to a typed MCP error otherwise.

---

## 7. User stories & acceptance criteria

**S1 [P0] Agent answers a metric question end-to-end.**
- AC1: Given a connected Postgres source and a canonical `revenue` binding, when an agent calls the MCP `query` tool with `{metrics:[revenue], dimensions:[order_date]}`, then it receives rows plus the metadata block (resolved binding, guardrails fired, freshness).
- AC2: The same query via `canon query --json` returns a byte-identical core payload.

**S2 [P0] Discovery.**
- AC1: When an agent calls `list_metrics`, then it gets the active canonical metrics; `describe_metric(revenue)` returns grain, dimensions, owning source, and freshness.

**S3 [P0] Ambiguity surfaces, no guessing.**
- AC1: Given two active bindings for a name, when `resolve_metric`/`query` is called, then the tool returns `AMBIGUOUS` with candidates and no result — the agent can refuse-and-ask.

**S4 [P0] Guardrail block is actionable.**
- AC1: Given a `required_dimension` guardrail, when the agent omits it, then the tool returns `GUARDRAIL_BLOCK` with the rationale, not a wrong answer.

**S5 [P0] Read-only escape hatch.**
- AC1: `run_sql`/`canon sql` executes a SELECT and rejects any non-SELECT with `READ_ONLY_VIOLATION`.

**S6 [P0] Daemon lifecycle.**
- AC1: `canon mcp start` brings the server up bound locally; `status` reports it; `stop` shuts it down. `canon status` tells the user to start it if an agent needs it and it's down.
- AC2: A CLI/daemon version mismatch fails with a clear message on start.

**S7 [P0] Verified client integration.**
- AC1: For each of Claude Code, Cursor, Codex, an integration test configures the MCP server, lists tools, calls `query`, and asserts the metadata block is present.

**S8 [P0] Headless CLI for CI.**
- AC1: `canon compile --json -f q.json` in a CI job returns exit `0` on success and the correct non-zero code on each structured error class (per the canonical error registry, §6.1).

**S9 [P0] Adapter parity is enforced.**
- AC1: The parity conformance test (§6) passes for `resolve`, `compile`, `query`, `run_sql`.

**S10 [P1] Knowledge search & propose.**
- AC1: `search_context` returns ranked knowledge/semantics hits.
- AC2: `propose_change` validates and stages a diff, writing only to context files.

---

## 8. Open questions (E7/E8-specific)

- **MCP transport:** stdio vs. local socket/HTTP for the daemon, and how each verified client expects to connect.
- **Auth to the daemon:** is local binding sufficient for v1, or is a local token needed (esp. once `propose` can write context)?
- **Result size handling over MCP:** large `ResultSet`s — truncate-with-pointer, paginate, or force a `LIMIT`? (interacts with cost control E13.)
- **Tool granularity — decided.** `compile` and `query` are separate (clearer for agents): `compile` returns `compiled`+`metadata`, `query` returns the full `QueryResult` (§2.2).
- **Streaming:** do any clients need streamed results in v1, or is batch sufficient?

---

## 9. Out of scope (this spec)

- Capability internals: introspection/execution (E2), resolution/compilation/enforcement (E5/E15), knowledge retrieval (E6), edit/propose internals (E9).
- Trust-score computation (E14) — surfaced metadata is passed through, not computed here.
- Cost budgets/caching (E13).
- Project setup/install wizard (E1) — only the command entry points are referenced.
- ACP implementation (deferred).
