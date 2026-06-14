# Spec — E2 Primary Source Connector

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-2, FR-3; §9.1 Phase 0)
**Related:** SPEC E5+E15 (consumes this connector's output and execution)
**Last updated:** 2026-06-13

E2 is the upstream dependency of the compiler: it produces the semantic-source evidence the compiler reads and executes the read-only SQL the compiler emits. This spec defines the **primary (queryable) connector class** and the **connector contract** all connectors implement. Phase 0 ships exactly one concrete connector: PostgreSQL.

Phase markers: **[P0]** walking skeleton, **[P1]** v1 core, **[L]** later.

---

## 1. Scope

In scope:
- The **connector contract**: capability interface, normalized evidence schema, lifecycle, conformance harness.
- The **primary (queryable) connector class**: `introspect_schema`, `read_query_history`, `run_read_only_sql`.
- The **schema acquisition ladder** (PRD FR-2) and **schema validation probe** (PRD FR-3).
- Read-only enforcement and connection security.
- Concrete **PostgreSQL** connector [P0].

Out of scope (separate specs): definition/evidence connector classes — dbt, BI, docs (E3); context building/reconciliation that consumes evidence (E4); the compiler (E5/E15); CLI/MCP transport (E7/E8).

---

## 2. Connector contract

A connector declares **capabilities**, not identity. The core dispatches on capability, never on vendor name. Capabilities map to the three classes from the PRD; this spec covers the *primary* class.

```text
Capability                  Class      Phase   Returns (normalized evidence)
─────────────────────────   ────────   ─────   ─────────────────────────────
introspect_schema()         primary    P0      RelationSchema[]
read_query_history(since)   primary    P1      ObservedQuery[]
run_read_only_sql(sql,opts) primary    P0      ResultSet
test_connection()           all        P0      Health
capabilities()              all        P0      Capability[]   # self-declaration
```

- A connector implements a subset; `capabilities()` advertises which. The core checks capabilities before invoking and degrades via the acquisition ladder (§4) when one is absent.
- **No vendor name in core logic.** Routing is by capability + connection type registered at the edge.
- Connectors are versioned independently of the core against a versioned contract (PRD FR-2).

### 2.1 Normalized evidence schema [P0]

Every connector translates native output into one internal shape. The context builder (E4) and compiler (E5) never see vendor-specific structures.

```yaml
RelationSchema:
  connection: warehouse_pg
  relation: analytics.fct_orders     # fully-qualified
  kind: table                        # table | view | materialized_view
  columns:
    - { name: order_id, type: string, nullable: false, position: 1 }
    # type ∈ normalized type set: string,int,decimal,float,bool,date,timestamp,json
  primary_key: [order_id]            # if discoverable, else []
  foreign_keys:                      # if discoverable
    - { columns: [customer_id], references: { relation: analytics.dim_customers, columns: [customer_id] } }
  row_count_estimate: 1043221        # nullable
  acquisition_tier: live             # which ladder tier produced this (§4): live | modeling | query_history | declarative | sample | hand_authored
  source_fingerprint: "sha256:…"     # over the normalized schema, for drift detection

ObservedQuery:                       # [P1]
  sql_normalized: "select … from analytics.fct_orders join …"
  relations: [analytics.fct_orders, analytics.dim_customers]
  joins_observed: [{ left: fct_orders.customer_id, right: dim_customers.customer_id }]
  frequency: 87                      # occurrences in window
  last_seen: "2026-06-12T12:00:00Z"

ResultSet:
  columns: [{ name, type }]
  rows: [[...]]
  truncated: false
  bytes_scanned: 10485760            # nullable; feeds cost control (E13)
```

Native→normalized type mapping is owned by the connector (inverse of the compiler's dialect adapter); unmappable types are recorded as `json`/`string` with a warning, never dropped silently.

### 2.2 Lifecycle [P0]

`add → test_connection → register`. A connection must pass `test_connection` before any context build (PRD FR-2). Operations: add, test, list, remove. Credentials live in `.canon/` (git-ignored); never written to committed files.

### 2.3 Conformance harness [P0]

A test kit asserts a candidate connector satisfies its class contract: declares capabilities truthfully, emits schema-valid normalized evidence, honors read-only, and round-trips a known fixture relation. Required to certify any connector (first-party or out-of-tree) without manual review.

---

## 3. Read-only enforcement [P0]

Non-negotiable; defense in depth:
1. **Connection-level:** connect with a read-only role/credential where the engine supports it; document the least-privilege grant per engine.
2. **Statement-level:** `run_read_only_sql` rejects any statement that is not a single `SELECT`/`WITH…SELECT` (parse-level check, not regex); no multiple statements, no DML/DDL.
3. **Session-level:** set read-only session flags where available (e.g. Postgres `default_transaction_read_only`), wrap in a read-only transaction.
4. **Limits:** enforce a row cap and statement timeout on every execution (hard ceiling; cost control E13 layers budgets on top).

A failure of any layer aborts with `READ_ONLY_VIOLATION`; the query never runs.

---

## 4. Schema acquisition ladder [P0 structure; tiers phased]

When `introspect_schema` is unavailable or partial, descend in priority order (PRD FR-2). All tiers emit the same `RelationSchema`; `acquisition_tier` records which was used (provenance).

| Tier | Method | Phase | Notes |
| --- | --- | --- | --- |
| 1 | Live introspection | P0 | catalog views (`information_schema`, `pg_catalog`) |
| 2 | Modeling code as schema | P1 | from dbt/LookML (via E3) |
| 3 | Query-history inference | P1 | from `read_query_history` |
| 4 | Declarative import | P0 | user supplies DDL / `information_schema` export / schema YAML |
| 5 | Sample-based inference | P1 | read-only `SELECT … LIMIT n`, infer columns/types |
| 6 | Hand-authored `semantics/*.yaml` | P0 | user authors directly; validated via probe (§5) |

- P0 must support **tier 1 (live)** and **tiers 4 & 6 (declarative/hand-authored)** so a source with blocked catalog access is still usable on day one.
- **Partial capability is never silent:** if only some relations are introspectable, the connector reports the gap and the core asks whether to proceed or supplement via a lower tier.

---

## 5. Schema validation probe [P0]

Whenever schema is acquired via tiers 4–6 (declarative, sample, hand-authored), issue a **read-only probe** against the live source before committing the evidence:
- Probe = `SELECT <declared columns> FROM <relation> WHERE false` (or `LIMIT 0`) to verify the relation and columns exist and types are compatible, with zero data scanned.
- Compare declared vs. observed; on mismatch return `SCHEMA_MISMATCH` with a diff (missing/extra columns, type conflicts). Never silently accept.
- On success, stamp `last_validated_at` and `source_fingerprint`.

---

## 6. PostgreSQL connector [P0]

The one concrete Phase-0 connector. (Also covers Postgres-compatible engines such as Redshift via the same catalog surface, to be confirmed in test.)
- **Capabilities:** `introspect_schema`, `run_read_only_sql`, `test_connection`, `capabilities`. (`read_query_history` via `pg_stat_statements` is **[P1]**.)
- **Introspection:** `information_schema.columns/tables`, `table_constraints`/`key_column_usage` for PK/FK, `pg_class.reltuples` for row estimates.
- **Type mapping:** Postgres types → normalized set (e.g. `numeric/decimal→decimal`, `timestamp/timestamptz→timestamp`, `jsonb→json`).
- **Read-only:** read-only transaction + `default_transaction_read_only`; recommended least-privilege grant documented.
- **Connection:** standard DSN/params; TLS supported; credentials in `.canon/`.

---

## 7. User stories & acceptance criteria

**S1 [P0] Connect and test.**
- AC1: Given valid Postgres credentials, when I add and test the connection, then `test_connection` returns healthy and the connection is registered.
- AC2: Given bad credentials, then test fails with a clear error and the connection is **not** registered (no context build possible).

**S2 [P0] Live introspection → normalized evidence.**
- AC1: Given a healthy Postgres connection, when I introspect, then each table yields a `RelationSchema` with columns (normalized types), PK/FK where discoverable, and `acquisition_tier: live`.
- AC2: Unmappable native types are recorded as `json`/`string` with a warning, never dropped.

**S3 [P0] Read-only is enforced.**
- AC1: Given any non-SELECT statement passed to `run_read_only_sql`, then it is rejected with `READ_ONLY_VIOLATION` before execution.
- AC2: Given multiple statements in one call, then rejected.
- AC3: Every execution applies the row cap and statement timeout.

**S4 [P0] Execute compiled SQL.**
- AC1: Given a SELECT from the compiler (E5), when executed, then a `ResultSet` is returned with typed columns and `bytes_scanned` populated where available.

**S5 [P0] Blocked catalog → declarative import.**
- AC1: Given introspection is unavailable, when I supply a DDL/schema export (tier 4), then valid `RelationSchema` evidence is produced with `acquisition_tier: declarative`.

**S6 [P0] Hand-authored schema is validated against reality.**
- AC1: Given a hand-authored `semantics/*.yaml` (tier 6), when committed, then a probe query runs and, on a column/type mismatch, returns `SCHEMA_MISMATCH` with a diff and blocks the commit.
- AC2: On match, `last_validated_at` and `source_fingerprint` are stamped.

**S7 [P0] Partial introspection is surfaced.**
- AC1: Given only some relations are introspectable, then the gap is reported and the user is asked to proceed or supplement; nothing is silently omitted.

**S8 [P0] Conformance harness certifies a connector.**
- AC1: Given a candidate connector, when run through the harness, then it passes only if capabilities are truthful, evidence is schema-valid, read-only holds, and the fixture round-trips.

**S9 [P1] Query-history extraction.**
- AC1: Given `pg_stat_statements` is available, when I read history since a timestamp, then `ObservedQuery` evidence (relations, observed joins, frequency) is produced.

---

## 8. Open questions (E2-specific)

- **Redshift parity:** confirm the Postgres catalog path covers Redshift `SVV_*`/late-binding views, or whether it needs a thin variant connector.
- **FK discovery reliability:** many warehouses don't enforce/declare FKs; how much do we lean on tier 3 (query-history joins) vs. declared FKs for the join graph the compiler needs?
- **Type-mapping edge cases:** arrays, enums, spatial, vendor-specific numerics — normalized representation and round-trip with the dialect adapter (E5 §5).
- **Probe cost on huge tables:** `WHERE false` is zero-scan on Postgres; confirm the equivalent is genuinely free on other P1 engines before generalizing.
- **Credential storage format** in `.canon/` and rotation (shared with PRD §10 secret-handling).

---

## 9. Out of scope (this spec)

- Definition/evidence connector classes (dbt, LookML, BI, Notion) — E3, on this same contract.
- Reconciliation/context building from evidence — E4.
- SQL compilation and the dialect adapter — E5/E15.
- Cost budgets and result caching — E13 (this spec only provides `bytes_scanned` and hard limits).
- CLI/MCP exposure of connection commands — E7/E8.
