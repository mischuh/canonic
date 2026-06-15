# Spec — P0 Serving-Contract Interface Freeze

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (§9.1 critical-path dependency; §10 connector-/contract-stability)
**Related:** SPEC E5+E15 (§3 semantic query, §4 output, §6 resolver seam), SPEC E7+E8 (§2.2 `QueryResult`, §6.1 error registry), SPEC E1 (config version), SPEC E2 (connector contract — separately versioned)
**Last updated:** 2026-06-15

This is the fifth Phase-0 companion document. It does not add behavior. It **freezes** the interface that all of Phase 1/2 serves through, as a single versioned contract, and defines the rule by which that contract may change. Per PRD §9.1, nothing in Phase 1/2 may be spec'd until this is fixed.

Phase markers: **[P0]** walking skeleton, **[P1]** v1 core, **[L]** later.

---

## 1. Scope

The **serving contract** `contract_schema: v1` — the request/response cycle every adapter and every downstream consumer (E4, E6, E10, E14, E16, third-party agent clients) depends on. Four surfaces, already specified individually, are pinned here as one unit because they form one cycle:

1. **Semantic query** — compiler input (SPEC E5 §3).
2. **`QueryResult` / compile output** — compiler output merged for serving (SPEC E7/E8 §2.2; SPEC E5 §4).
3. **Canonical error registry** — structured errors + exit codes (SPEC E7/E8 §6.1).
4. **`ContractResolver` hooks** — the internal E15↔E5 seam (SPEC E5 §6).

In scope: the frozen field set of each surface, the version-and-compatibility policy, the conformance gate that enforces the freeze, and the change process.

Out of scope (and deliberately **not** frozen): see §6.

---

## 2. What "frozen" means

A frozen surface has a **pinned field set** and a **schema snapshot** checked into the repo as a golden file. Any change to the field set — add, remove, rename, retype — fails the conformance gate (§5) unless it is accompanied by an explicit, reviewed version change per §4. The freeze is therefore an *operational* mechanism (snapshot tests), not a promise in prose: drift cannot happen silently.

The contract is consumed by two populations:
- **In-tree consumers** (E4, E6, E10, E14, E16) — versioned in lockstep with the core; update together.
- **Out-of-tree consumers** (agent clients over MCP, external tooling reading the `--json` payload) — versioned independently. This population is the reason compatibility must be a rule, not a convention.

The serving contract is distinct from the **connector contract** (SPEC E2, FR-2), which is versioned separately. This document does not freeze the connector contract — that one is still learning from the first concrete connectors (PRD §10) and freezes later.

---

## 3. The four frozen surfaces [P0]

Canonical shapes are defined in the source specs; restated here so this document is the single point of reference for the freeze. The `[P1]`/`[L]` fields already present in the source schemas are **reserved**, not invented-on-demand: they are part of the v1 field set so that filling them in later is additive (§4), not a breaking change.

### 3.1 Semantic query (request) — SPEC E5 §3
Frozen fields: `metrics`, `dimensions`, `filters`, `context`, `limit`. References names only, never physical tables/columns. Adapters produce it; the compiler never sees plain language.

### 3.2 `QueryResult` / compile output (response) — SPEC E7/E8 §2.2, SPEC E5 §4
Frozen top-level shape:
- `query` → `result` (the E2 `ResultSet`) + `compiled` (`sql`, `dialect`) + `metadata`.
- `compile` → `compiled` + `metadata` (no `result`).
- `metadata` is exactly the E5 compiler metadata: `resolved`, `guardrails_fired[]`, `finality`, `freshness[]`, `warnings[]`. **No surface adds or renames fields.**

### 3.3 Canonical error registry — SPEC E7/E8 §6.1
Frozen: every `{code, message, candidates?}` shape and every existing `code → exit` mapping (`UNRESOLVED`=2 … `CONNECTION_ERROR`=13, `0`=success). The set of codes may **grow** under §4; existing codes and exits are immutable.

### 3.4 `ContractResolver` hooks (internal seam) — SPEC E5 §6
Frozen signatures: `resolve_metric(name, context)`, `guardrails_for(source, measure, ctx)`, `finality_for(metric)`, `assertions_for(query)`. The resolver returns deterministic, stable-sorted results. This is the *internal* half of the contract (E15 declares, E5 enforces); it does not cross the wire, but it is part of the freeze because extending guardrails touches it (§4.3).

---

## 4. Versioning & compatibility policy [P0]

One version, `contract_schema`, covers all four surfaces, because a single change (e.g. a new guardrail kind) can touch several of them at once. SemVer-style: `MAJOR.MINOR`.

### 4.1 Additive — MINOR bump, no consumer break
A change is additive iff existing consumers keep working unchanged:
- A new **optional** field in the semantic query (compiler supplies a default).
- A new **optional** field in `QueryResult.metadata` (unknown fields are ignored by consumers).
- A new **error code** appended to the registry with a new exit value (existing codes/exits untouched).
- A new **guardrail kind** (see §4.3).
- Filling a reserved `[P1]`/`[L]` field already in the v1 field set.

### 4.2 Breaking — MAJOR bump
- Removing, renaming, or retyping any field in the query or `QueryResult`.
- Changing the meaning or exit value of an existing error code.
- Changing the `result`/`compiled`/`metadata` nesting.
- Changing a `ContractResolver` hook signature or its determinism guarantee.

### 4.3 New guardrail kind — decided
SPEC E5 §6 requires a version bump when a guardrail *kind* is added; SPEC E5 §10 leaves *how* open. **Decision: it is a MINOR bump (additive), not MAJOR.** Rationale: the wire shapes do not change — a new-kind guardrail still fires as a `guardrails_fired[]` entry and, when blocking, still returns the existing `GUARDRAIL_BLOCK` code with its `rationale`. A consumer never needs kind-specific logic: the structured error plus rationale is sufficient to refuse-and-ask (the design already surfaces rationale precisely for this). The internal resolver change ships in lockstep in-tree. So `required_dimension`, `restrict_source`, finality, and assertions (all reserved `[P1]`) arrive as minor bumps.

### 4.4 Where the version is stamped & checked
- **Advertised** by the MCP server at handshake/tool-list and by `canon status`, so a client or CI can read the server's `contract_schema` before relying on it.
- **Echoed** in `QueryResult.metadata` so any persisted result (E16 event log) records which contract produced it — provenance for accuracy tracking.
- **Negotiated at connect, not per query.** A client declares the MAJOR it was built against. The daemon accepts iff `client.major == server.major` and `server.minor >=` the minor introducing any feature the client uses; otherwise it fails fast with a clear message — same pattern as the CLI↔daemon binary-version check (SPEC E7/E8 §4.2). This is a connect-time check, so it adds **no** new registry code.

---

## 5. Conformance gate [P0]

The freeze is enforced by promoting existing acceptance criteria to **regression gates** in CI, plus one new snapshot test:

1. **Schema snapshot (new).** The JSON schema of the semantic query, `QueryResult`, compile output, and error registry is held as golden files. A diff fails CI unless the same PR updates the golden file **and** bumps `contract_schema` per §4. This is the mechanism that makes the freeze real.
2. **Determinism.** Byte-identical SQL + identical `guardrails_fired` ordering for a repeated query (SPEC E5 S5).
3. **Adapter parity.** Byte-identical core payload between `canon … --json` and the MCP `query` tool, for `resolve`/`compile`/`query`/`run_sql` (SPEC E7/E8 §6, S9).
4. **Error→exit mapping.** Each structured error class yields its registry exit code in headless mode (SPEC E7/E8 S8).
5. **Resolver determinism.** `ContractResolver` returns stable-sorted, identical results for identical inputs (SPEC E5 §6).

Passing all five against `contract_schema: v1` is the formal Phase-0 exit for the interface and the precondition for spec'ing E4/E6/E10.

---

## 6. Out of scope — deliberately NOT frozen

To keep the freeze tight and avoid blocking Phase 1, the following evolve under their own specs' validation, independent of `contract_schema`:
- **Compiler internals** — stage ordering and implementation (SPEC E5 §4). Only the input/output shapes are frozen, not how stages work.
- **Dialect adapter internals** and which dialects exist (SPEC E5 §5). Adding a dialect does not touch the serving contract.
- **On-disk file schemas** — `semantics/*.yaml`, `contracts/**/*.yaml`, knowledge frontmatter. These are authored by E4/E6 and validated by E5/E15; they may gain fields without changing the *serving* contract, because an agent's query and the `QueryResult` shape are independent of whether a semantic source grew a column. (Their evolution still follows §4-style additive discipline within E5/E15, but under a separate file-schema version, not `contract_schema`.)
- **Connector contract** (SPEC E2) — separately versioned; freezes later (PRD §10).
- **Trust-score weighting** (E14), **cost/caching** (E13) — consume frozen outputs, do not define them.

---

## 7. Change process

1. Open a short ADR/RFC describing the change and classifying it MINOR or MAJOR per §4.
2. Update the affected source spec(s) and the golden schema snapshot(s) in the same PR.
3. Bump `contract_schema`; add a line to the contract CHANGELOG.
4. CI runs the §5 gate against the new version; reviewers confirm the classification.

A MAJOR bump is a deliberate, reviewed event — expected to be rare after v1, since the reserved `[P1]`/`[L]` fields mean the known roadmap is additive.

---

## 8. User stories & acceptance criteria

**S1 [P0] The contract is a single versioned unit.**
- AC1: `canon status` and the MCP handshake both report a `contract_schema` version.
- AC2: Every `QueryResult.metadata` carries the `contract_schema` that produced it.

**S2 [P0] Silent drift is impossible.**
- AC1: Given a PR that adds a field to `QueryResult` without updating the golden snapshot, then the conformance gate fails.
- AC2: Given the same PR with the golden snapshot updated but no version bump, then the gate still fails until `contract_schema` is bumped per §4.

**S3 [P0] Additive change does not break existing consumers.**
- AC1: Given a MINOR bump that appends a new error code, when a client built against the prior MINOR runs against the new daemon, then existing capabilities behave identically and existing exit codes are unchanged.
- AC2: Given a new guardrail kind (MINOR, §4.3), when an unaware client triggers it, then it receives `GUARDRAIL_BLOCK` with rationale and can refuse-and-ask without kind-specific logic.

**S4 [P0] Major mismatch fails fast.**
- AC1: Given a client declaring a different `contract_schema` MAJOR than the daemon, when it connects, then the daemon refuses with a clear version-mismatch message (connect-time, no per-query error code).

**S5 [P0] Parity and determinism hold at the frozen version.**
- AC1: The §5 gate (snapshot, determinism, parity, error→exit, resolver determinism) passes against `contract_schema: v1`.

---

## 9. Open questions (freeze-specific)

- **Freeze `v1` now vs. one more iteration?** P0 proved the serving contract end-to-end, and the reserved fields cover the known P1 roadmap, so freezing `v1` now is low-risk — unlike the connector contract (PRD §10), which is not frozen here. Confirm no P1 consumer (E4/E6/E14/E16) needs a query/result field not already reserved.
- **Unified vs. split version.** One `contract_schema` covers wire + resolver seam. If the internal resolver ever changes on a different cadence than the wire surfaces, revisit splitting into a wire version and an internal-interface version.
- **Negotiation depth over MCP.** §4.4 specifies a connect-time MAJOR check; whether clients also negotiate MINOR feature flags interacts with the E8 transport/auth open questions (SPEC E7/E8 §8).
- **File-schema version surface.** Confirm the on-disk file schemas carry their own version distinct from `contract_schema`, owned by E5/E15, so E4/E6 authoring evolves without touching the serving freeze.
