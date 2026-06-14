# Spec — E5 Semantic Layer & Compiler + E15 Contract Surface

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-4, FR-13; §9.1 Phase 0)
**Last updated:** 2026-06-13

E5 and E15 are specified together because the compiler (E5) *enforces* what the contract surface (E15) *declares* — they are two halves of one interface. Everything in PRD Phase 1/2 serves through this interface, so it must be fixed first.

Phase markers: **[P0]** = walking skeleton, **[P1]** = v1 core, **[L]** = later. The schema is defined in full; only marked fields are required for P0.

---

## 1. Scope

In scope:
- The `semantics/*.yaml` schema (semantic sources): grain, columns, measures, dimensions, joins, filters, segments, finality metadata.
- The `contracts/` schemas: metric bindings, guardrails, finality rules, assertions. (Access policy and drift detection are defined at interface level but implemented in E12/E11.)
- The **semantic query** format (compiler input).
- The **compiler**: resolution → planning → enforcement → SQL emission, with a provisional/final result attribute.
- The **contract↔compiler enforcement interface**.
- Validation and determinism requirements.

Out of scope (separate specs): ingestion/authoring of these files (E4/E6), MCP/CLI transport (E7/E8), trust-score computation (E14, consumes our outputs), access-policy enforcement internals (E12).

---

## 2. Data model

### 2.1 Semantic source — `semantics/<connection-id>/<name>.yaml`

Describes one queryable table/relation the way an agent can reason about it.

```yaml
name: orders                      # [P0] unique within connection
connection: warehouse_pg          # [P0] which primary connector
table: analytics.fct_orders       # [P0] physical relation
grain: [order_id]                 # [P0] row uniqueness; drives fanout safety
description: "One row per order."  # [P1]

columns:                          # [P0]
  - { name: order_id,    type: string,    nullable: false }
  - { name: customer_id, type: string,    nullable: false }
  - { name: status,      type: string,    nullable: false }
  - { name: amount,      type: decimal,   nullable: false }
  - { name: created_at,  type: timestamp, nullable: false }

measures:                         # [P0] additive aggregations only in P0
  - name: total_revenue
    expr: "sum(amount)"           # P0: sum | count(*) | min | max
    additivity: additive          # additive [P0] | semi_additive [P1] | non_additive [P1]
    semi_additive_over: []        # [P1] dims over which a semi-additive measure is NOT additive (e.g. [date])
  - name: order_count
    expr: "count(distinct order_id)"
    additivity: non_additive      # [P1] declarable now, compilable in P1 (distinct counts do not sum)

dimensions:                       # [P0]
  - { name: status,     column: status }
  - { name: order_date, column: created_at, granularity: day }   # [P1] time granularity

joins:                            # [P0]
  - to: customers                 # another semantic source `name`
    on: "orders.customer_id = customers.customer_id"
    relationship: many_to_one     # one_to_one | many_to_one | one_to_many | many_to_many

filters:                          # [P1] named reusable predicates
  - { name: completed, expr: "status = 'completed'" }

segments: []                      # [L] named row subsets for analysis

finality:                         # [P1] see §2.4
  watermark: null                 # null = always-final source

meta:                             # [P0] system-managed, not hand-edited
  provenance: inferred            # board_approved | human_curated | inferred
  source_fingerprint: "sha256:…"  # of the introspected/declared schema
  last_validated_at: "2026-06-13T00:00:00Z"
```

**Typing.** `type` uses a normalized internal type set (`string, int, decimal, float, bool, date, timestamp, json`), mapped to dialect types by the dialect adapter (§5), never hard-coded per source.

### 2.2 Contract — canonical metric binding — `contracts/metrics/<metric>.yaml`

Resolves a logical metric *name* to exactly one owning measure. This is what an agent's plain-language request maps onto.

```yaml
metric: revenue                   # [P0] the name agents/users say
owner: "@data-platform"           # [P1] accountable team
canonical:                        # [P0] the single source of truth
  source: orders
  measure: total_revenue
provenance: human_curated         # [P0]
aliases: ["net revenue", "rev"]   # [P1] strings that resolve to this metric
deprecated_alternatives:          # [P1] known other definitions, explicitly NOT canonical
  - { source: metabase, ref: "question:412", reason: "gross, includes refunds" }
status: active                    # active | deprecated
```

Ambiguity rule **[P0]**: if a requested name matches zero or more than one active binding, the compiler does **not** guess — it returns a structured `AMBIGUOUS`/`UNRESOLVED` error listing candidates (consumed by refuse-and-ask upstream).

### 2.3 Contract — guardrail — `contracts/guardrails/<id>.yaml`

Declares a rule the compiler must enforce. Three kinds in scope:

```yaml
id: revenue-excludes-refunds
applies_to: { source: orders, measure: total_revenue }   # or { metric: revenue }
kind: mandatory_filter            # mandatory_filter | required_dimension | restrict_source
filter: "status != 'refunded'"    # for mandatory_filter
severity: error                   # error (block) | warn (annotate)
rationale: "Refunds are reversals, not revenue."          # surfaced to the agent
phase: P0
```

- `mandatory_filter` **[P0]** — predicate always AND-ed into the compiled WHERE.
- `required_dimension` **[P1]** — query must group by / filter on a given dimension or be rejected (e.g. multi-currency `amount` requires `currency`).
- `restrict_source` **[P1]** — in a given context (e.g. board reporting), only the final source is permitted (ties to finality §2.4).

### 2.4 Contract — finality rule — `contracts/guardrails/finality-<metric>.yaml` **[P1]**

Models one logical metric realized by two physical sources along a finality axis (the batch-vs-real-time worked example from the PRD).

```yaml
metric: revenue
realizations:
  - { source: orders,         role: final,    watermark: "business_day - 1 day", tz: "America/New_York" }
  - { source: orders_rt,      role: provisional }
coalescing: "window <= watermark ? final : provisional"   # which source serves which time window
result_flag: per_row            # every result row tagged final|provisional
board_only_final: true          # final-only enforced where restrict_source(board) applies
```

### 2.5 Contract — assertion — `contracts/assertions/<id>.yaml` **[P1]**

A trusted query→expected-result check. Used as a CI gate (headless) and a compile-time regression oracle.

```yaml
id: revenue-2025-q1
query: { metrics: [revenue], filters: ["order_date in 2025-Q1"] }
expect: { rows: 1, values: { revenue: 4218334.10 }, tolerance: 0.01 }
source_of_truth: "Finance close, FY25 Q1"
```

---

## 3. Semantic query (compiler input) **[P0]**

The protocol-neutral request the compiler resolves. Adapters (MCP/CLI) produce it; the compiler never sees plain language.

```json
{
  "metrics":   ["revenue"],
  "dimensions":["order_date", "status"],
  "filters":   ["status = 'completed'", "order_date >= '2025-01-01'"],
  "context":   "board_reporting",
  "limit":     1000
}
```

- `metrics` resolve via canonical bindings (§2.2); `dimensions`/`filters` resolve against the owning source and reachable joins.
- `context` is an optional tag that activates context-scoped guardrails (e.g. `restrict_source`).
- The query references **names**, never physical tables/columns — those are resolved by the compiler.

---

## 4. Compiler pipeline **[P0 unless noted]**

Deterministic, no LLM. Ordered stages; each either advances or returns a structured error.

1. **Resolve metrics.** Map each metric name → canonical binding → `(source, measure)`. Unknown/ambiguous → `UNRESOLVED`/`AMBIGUOUS` error with candidates.
2. **Resolve dimensions & filters.** Bind to columns on the owning source or a join-reachable source. Unreachable → `UNREACHABLE` error.
3. **Plan join graph.** From the owning source, find the minimal join path to every referenced source using declared `joins`. No path / ambiguous path → error (no implicit cartesian joins, ever).
4. **Fanout analysis.** Detect when a join fans out the grain (one→many or many→many) relative to a measure's source grain. **[P0]** For an `additive` measure across fanout, deduplicate to the measure's grain before aggregating. A request for a `non_additive`/`semi_additive` measure returns `UNSUPPORTED_MEASURE` in P0; **[P1]** their fanout-safe handling (reject-if-corrupting) is added with non-additive support.
5. **Apply finality & coalescing [P1].** If the metric has a finality rule, select source(s) per the coalescing rule for the requested time window; mark output rows `final`/`provisional`.
6. **Enforce guardrails.** AND-in mandatory filters; check required dimensions; apply `restrict_source` for the active `context`. `severity: error` blocks; `warn` annotates. (Interface in §6.)
7. **Emit SQL.** Build the dialect-agnostic query AST, then transpile via the dialect adapter (§5). Read-only (SELECT) only.
8. **Attach result attributes.** Return SQL + metadata: resolved bindings used, guardrails fired, provisional/final mix, per-source freshness (`last_validated_at` + `stale` flag from each source's `meta`), and additivity handling applied. (Consumed by the trust score, E14.)
9. **Assertion check [P1].** In benchmark/CI mode, run the emitted SQL and compare to the assertion's `expect`; divergence beyond tolerance → fail.

Output object:

```json
{
  "sql": "SELECT …",
  "dialect": "postgres",
  "resolved": { "metrics": {"revenue": "orders.total_revenue"} },
  "guardrails_fired": [{"id": "revenue-excludes-refunds", "kind": "mandatory_filter"}],
  "finality": {"final_rows": "<=watermark", "provisional_rows": ">watermark"},
  "freshness": [{"source": "orders", "last_validated_at": "2026-06-13T00:00:00Z", "stale": false}],
  "warnings": []
}
```

Errors are structured (`code`, `message`, `candidates?`), never free text, so upstream can act on them programmatically.

---

## 5. Dialect adapter **[P0]**

- The compiler builds a dialect-neutral AST; a dialect adapter transpiles it. Decoupled from source connectors (PRD FR-2): adding a DB and supporting its SQL are independent.
- Adapter responsibilities: type mapping (internal type set → dialect types), identifier quoting, function/dialect quirks, `LIMIT`/pagination, read-only guarantee.
- **P0 dialect:** PostgreSQL (covers the Phase 0 connector). Others added behind the same interface; coverage list is an open question (PRD §10).

---

## 6. Contract ↔ compiler interface (the seam) **[P0]**

The single integration point between E15 (declares) and E5 (enforces). The compiler asks the contract layer for applicable rules at well-defined hook points; it never reads contract files directly per-rule.

```text
ContractResolver (E15) exposes to the compiler (E5):
  resolve_metric(name, context)        -> Binding | Ambiguous | Unresolved
  guardrails_for(source, measure, ctx) -> [Guardrail]      # ordered, deterministic
  finality_for(metric)                 -> FinalityRule | None
  assertions_for(query)                -> [Assertion]
```

Rules:
- The resolver returns **deterministic, ordered** results (stable sort) so identical queries compile identically — required for caching and assertions.
- The compiler treats the resolver as the only authority on "what is canonical / what must be obeyed"; no canonicality logic lives in the compiler.
- Adding a new guardrail *kind* is a change to both sides of this interface and must bump the contract-schema version.

---

## 7. Validation **[P0]**

Run on write and before serving (PRD FR-4/FR-13):
- **Semantic source:** referenced columns exist in `columns`; join targets exist; `grain` columns exist; measure `expr` references only declared columns; type consistency.
- **Binding:** `canonical.source`/`measure` exist; no two active bindings share a metric name or alias; deprecated alternatives are syntactically valid.
- **Guardrail/finality:** `applies_to` targets resolve; finality `realizations` reference existing sources; exactly one `final` role per metric.
- **Cross-surface:** every contract reference points at a live semantic entity (catches drift — PRD FR-13).
- Validation failure is a hard error with a precise location; never a silent skip.

---

## 8. Determinism & headless **[P0]**

- Given identical `semantics/`, `contracts/`, and semantic query, the compiler emits byte-identical SQL. No randomness, no LLM, no wall-clock except where the query itself is relative-dated (resolved from an explicit `as_of` when provided).
- Headless/CI invocation returns process exit codes: `0` success, non-zero per error class (`UNRESOLVED`, `AMBIGUOUS`, `UNREACHABLE`, `AMBIGUOUS_JOIN_PATH`, `UNSUPPORTED_MEASURE`, `FANOUT_UNSAFE`, `GUARDRAIL_BLOCK`, `VALIDATION_FAILED`, `ASSERTION_FAILED`). The canonical code→exit mapping lives in SPEC E7+E8 §6.1. Enables the §9.1 CI-gate role.

---

## 9. User stories & acceptance criteria

Format: As a consumer, … / **AC** in Given–When–Then. Phase in brackets.

**S1 [P0] Compile a simple metric.**
As an agent, I request `revenue` by `order_date` so I get correct SQL.
- AC1: Given a canonical binding `revenue→orders.total_revenue`, when I submit `{metrics:[revenue], dimensions:[order_date]}`, then the compiler emits read-only Postgres SQL grouping `sum(amount)` by day.
- AC2: Given no matching binding, then it returns `UNRESOLVED` with no SQL.
- AC3: Given two active bindings for `revenue`, then it returns `AMBIGUOUS` listing both candidates and emits no SQL.

**S2 [P0] Enforce a mandatory-filter guardrail.**
- AC1: Given guardrail `revenue-excludes-refunds` (mandatory_filter, error), when I compile `revenue`, then `status != 'refunded'` is AND-ed into WHERE and `guardrails_fired` lists it.
- AC2: The filter is applied even if the request already filters status differently (both AND-ed).

**S3 Fanout handling.**
- AC1 **[P0]**: Given an `additive` measure across a one_to_many join, when compiling, then the compiler deduplicates to the measure's source grain before summing and succeeds.
- AC2 **[P0]**: Given a `non_additive`/`semi_additive` measure, then the compiler returns `UNSUPPORTED_MEASURE` (deferred to P1) — never a silently inflated number.
- AC3 **[P1]**: With non-additive support, a non-additive measure across corrupting fanout is rejected with `FANOUT_UNSAFE` and an explanation.

**S4 [P0] No implicit or ambiguous joins.**
- AC1: Given a dimension on a source with no declared join path to the metric's source, then `UNREACHABLE` is returned; the compiler never emits a cross join.
- AC2: Given more than one valid join path between the referenced sources, then `AMBIGUOUS_JOIN_PATH` is returned and the query must name an explicit path — no shortest-path guessing.

**S5 [P0] Deterministic output.**
- AC1: Compiling the same query twice yields byte-identical SQL and identical `guardrails_fired` ordering.

**S6 [P0] Read-only & dialect-correct.**
- AC1: Emitted SQL is SELECT-only; any attempt to compile a mutating operation is impossible by construction.
- AC2: Internal types map to valid Postgres types; identifiers are correctly quoted.

**S7 [P1] Finality-aware revenue.**
- AC1: Given the `revenue` finality rule, when I query "last 7 days" and today > watermark, then 6 days resolve to `orders` (final) and 1 day to `orders_rt` (provisional), and result rows are flagged accordingly.
- AC2: Given `context: board_reporting` with `board_only_final`, then the provisional source is excluded and the result is final-only.

**S8 [P1] Assertion as regression oracle / CI gate.**
- AC1: Given assertion `revenue-2025-q1`, when run in benchmark mode, then the compiled query executes and the value matches within tolerance, else `ASSERTION_FAILED` with expected vs. actual.
- AC2: In headless mode a failed assertion produces a non-zero exit code.

**S9 [P1] Required-dimension guardrail.**
- AC1: Given a `required_dimension: currency` guardrail on `revenue`, when I omit `currency`, then `GUARDRAIL_BLOCK` is returned with the rationale.

**S10 [P0] Validation catches drift.**
- AC1: Given a binding pointing at a measure that no longer exists, then validation fails with the exact file/line before any serving.

---

## 10. Open questions (E5/E15-specific)

- **Measure expression language — decided.** P0 supports **additive aggregations only**: `sum`, `count(*)`, `min`, `max` over declared columns. Non-additive (`avg`, `count(distinct …)`, ratios) and windowed measures are **deferred to P1**. This keeps the P0 fanout logic simple (additive dedup only) and avoids the hardest correctness cases up front.
- **Join-path ambiguity — decided.** When multiple valid join paths exist, the compiler **rejects** with `AMBIGUOUS_JOIN_PATH` and requires the query to name an explicit path. No shortest-path or any other implicit guessing.
- **`many_to_many` joins:** support via an explicit bridge declaration in P1, or reject in P0? (Leaning: reject in P0.)
- **Relative dates:** standard `as_of` handling and timezone source for "last 7 days" semantics.
- **Contract-schema versioning:** how the resolver interface (§6) versions when a new guardrail kind is added.
- **Dialect coverage order** after Postgres (Snowflake/BigQuery) — inherits PRD §10.

---

## 11. Out of scope (this spec)

- Plain-language → semantic-query translation (agent/adapter side).
- Trust-score weighting (E14) — we only emit the inputs.
- Access-policy/RLS/PII enforcement internals (E12) — `restrict_source` is the only context-gating primitive defined here.
- Ingestion/auto-authoring of semantics and contracts (E4/E6).
- Result caching (E13) — relies on our deterministic SQL but is specified separately.
