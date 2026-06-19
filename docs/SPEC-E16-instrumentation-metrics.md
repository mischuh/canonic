# Spec — E16 Instrumentation & Metrics

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-14; §8 success metrics; §9.1 Phase 1 minimal / Phase 2 full)
**Related:** SPEC E1 (`.canon/` event-log location, telemetry config), SPEC E5+E15 (compiler metadata + assertions as the accuracy oracle), SPEC E7+E8 (`QueryResult` metadata + `contract_schema` source), SPEC E10 (LLM usage metrics), SPEC E4 (reconciliation events). Consumed/extended later by E11 (feedback), E13 (cost/cache), E14 (trust score).
**Last updated:** 2026-06-15

E16 is the measurement substrate that lets the product **prove** its success metrics (PRD §8) instead of asserting them. It owns the local event log every served answer and reconciliation decision writes to, the accuracy harness that turns assertions into a tracked accuracy number, the metrics derived from the log, and the opt-in aggregate telemetry. Nothing leaves the machine unless telemetry is explicitly enabled.

Phase markers: **[P1]** = Phase-1 *minimal E16* — the event log so accuracy is measurable from day one. **[L]** = Phase-2 *full E16* — accuracy harness, outcome capture, derived-metric reporting, opt-in telemetry. (PRD §9.1 makes this split explicit; in this spec [L] means "Phase 2," not "indefinitely deferred.")

---

## 1. Scope

In scope:
- The **event log substrate [P1]**: append-only, local, structured; the format and storage every producer writes to.
- The **served-answer event schema [P1]** and the reconciliation-decision event (shared substrate with E4 §6).
- **Local inspection [P1]**: `canon status`/report reads the log without any telemetry.
- The **accuracy harness [L]**: labeled question set → compile/execute → compare to assertions → tracked accuracy, re-runnable in CI.
- **Outcome capture [L]**: analyst correct/incorrect marks + corrections, logged as the ground-truth feed.
- **Derived metrics [L]** computable purely from the local log.
- **Opt-in aggregate telemetry [L]**: off by default, disclosed, anonymized, never warehouse content.

Out of scope (own specs):
- **Trust-score computation** — E14. E16 *logs* the score; it does not compute it.
- **Cost control / caching** — E13. E16 *logs* `bytes_scanned`, blocked-over-limit, and cache hits; it does not enforce budgets.
- **Acting on outcomes** (feeding reconciliation) — E11. E16 *records* outcome events; E11 consumes them.
- **Reconciliation decisions themselves** — E4 produces them; E16 owns the substrate they're written to.
- **LLM usage measurement** — E10 surfaces token/call/latency; E16 records it (§10).
- The compiler and serving paths (E5/E7/E8) — they *emit* events; E16 does not change them.

---

## 2. Event log substrate [P1]

An append-only, structured, **local** log under `.canon/` (git-ignored per E1 §7). It is the single substrate every producer writes to; consumers (inspection, harness, telemetry) read from it.

- **Local by default, nothing leaves the machine** unless opt-in telemetry is enabled (§8). Compatible with air-gapped operation (E10 §4) by construction.
- **No warehouse content, ever.** The log stores **hashes and structural metadata**, never result rows or literal values. The semantic query and compiled SQL are recorded as `sha256` hashes, not text, so filter literals (dates, ids) never land on disk. Resolved bindings (metric→source), guardrail ids, and counts are metadata and are stored.
- **Append-only:** events are immutable once written; corrections are new events, not edits (preserves the audit trail — PRD NFR reviewability).
- Storage format: newline-delimited structured records (one event per line) so it is streamable, greppable, and cheap to append; rotated by size/age (§12 open question).

---

## 3. Event schema [P1]

One record per served answer. Producers (the serving path E5/E7/E8) emit it; E16 owns the shape.

```yaml
AnswerEvent:                          # [P1]
  ts: "2026-06-15T12:00:00Z"
  kind: served_answer
  contract_schema: v1                 # which frozen contract produced this (freeze §4.4)
  query_hash: "sha256:…"              # the semantic query (hashed, never stored verbatim)
  compiled_sql_hash: "sha256:…"       # compiled SQL (hashed)
  connection: warehouse_pg
  resolved: { metrics: { revenue: orders.total_revenue } }   # bindings used
  guardrails_fired: [revenue-excludes-refunds]
  finality: { final_rows: 6, provisional_rows: 1 }           # null when no finality rule
  freshness: [{ source: orders, stale: false, age_days: 2 }]
  latency_ms: 142
  bytes_scanned: 10485760             # from E2 ResultSet; null when unavailable
  error: null                         # or a canonical registry code if the answer failed
  # ── reserved: present in the schema, populated when the producing epic lands ──
  trust_score: null                   # [L] from E14
  cache_hit: null                     # [L] from E13
  over_limit_blocked: null            # [L] from E13
```

- **Reserved fields** follow the freeze discipline (SPEC P0-interface-freeze §3): `trust_score`, `cache_hit`, `over_limit_blocked` are part of the v1 event shape now and populated only once E14/E13 exist — so adding them later is not a schema break and the field is computable the moment its producer ships.
- A **reconciliation event** (`kind: reconcile_decision`) shares this substrate: add/edit/prune/contradiction/no-op with tier, confidence, and anchored evidence — this is the E4 §6 event log, written here so one store backs both serving and ingest traceability.
- Every field maps to a PRD §8 metric (§7); recording them in P1 is what makes those metrics computable later, even before the harness exists.

---

## 4. Local inspection [P1]

`canon status` / `canon report` reads the local log and shows basic figures **without enabling any telemetry** (FR-14 inspectable):
- Recent served answers, counts, error-code distribution, latency and `bytes_scanned` summaries, freshness/guardrail-coverage at a glance.
- This is the minimal P1 read path: the log is not a black box on day one. Full derived-metric reporting is [L] (§7).

---

## 5. Accuracy harness [L]

The mechanism behind the ">90% accuracy" claim (PRD §8). A repeatable benchmark mode:
- Runs a **labeled question set** (semantic queries + expected results) through `compile` (and `query` where execution is needed).
- Compares output to **E15 assertions** / known-correct values within tolerance — generalizing the single-assertion CI check (E5 S8) into a suite that yields a tracked **accuracy number**.
- **Re-runnable in CI** so accuracy is tracked over time, not asserted once; a regression returns a non-zero exit (canonical registry, `ASSERTION_FAILED`).
- Reports accuracy against a **schema-only baseline** (PRD §8) so the lift from the context layer is provable.

Depends on E15 assertions ([P1]) and is the Phase-2 companion to E14's trust score (calibration, §7).

---

## 6. Outcome capture [L]

Records the FR-9 ground-truth feed as events:
- Analyst **correct/incorrect** marks on a served answer, and **corrections** applied to compiled SQL or a definition.
- Logged as `kind: answer_outcome`, linked to the originating `AnswerEvent` by `query_hash`/`compiled_sql_hash`.
- These events are the input E11 (Phase 2) consumes as evidence — consistent with E4 §11 ("answer-correctness outcomes are just another `EvidenceItem.kind`"). E16 **records**; E11 **acts**.

---

## 7. Derived metrics [L]

All computable from the local log, no external service (FR-14). Each metric maps to event fields and the epic that must produce them:

| PRD §8 metric | Computed from | Producer gating it |
| --- | --- | --- |
| Query accuracy vs. schema-only baseline | harness runs + assertions | §5, E15 |
| Share answerable without human SQL | served vs. outcome (human-authored) events | §6, E11 |
| Time-to-first-correct-answer | answer + outcome events | §6 |
| Context freshness lag | `freshness.age_days` | E6/E5 (recorded P1) |
| Contradiction detection rate | `reconcile_decision` events | E4 |
| Correction recurrence (↓) | repeated `answer_outcome` on same binding | §6, E11 |
| Guardrail/assertion coverage of high-risk measures | `guardrails_fired` + static contract scan | E15 |
| Share of answers with accurate freshness/guardrail caveat | `guardrails_fired`/`freshness` presence | recorded P1 |
| Cost/bytes per answer; cache hit; blocked-over-limit | `bytes_scanned`, `cache_hit`, `over_limit_blocked` | E13 (reserved) |
| Trust-score calibration (low score ↔ wrong) | `trust_score` × `answer_outcome` | E14, §6 |

The raw inputs for most of these are recorded in P1; the *computation/report* is the Phase-2 deliverable.

---

## 8. Opt-in aggregate telemetry [L]

Privacy-conscious, disclosed, **off by default** (E1 `telemetry.enabled: false`):
- **Aggregate/anonymized only** — counts, distributions, latencies. **Never** query results, warehouse content, hashes that could re-identify a query, or SQL.
- A **documented schema** of exactly what is sent, and a clear opt-out.
- **Incompatible with air-gapped** (E10 §4 forces it off): if `runtime.air_gapped: true`, telemetry cannot be enabled.

---

## 9. Modes & privacy

- **Headless/CI:** the event log is still written (the harness is itself a headless capability returning exit codes); no LLM needed for logging or for the harness comparison.
- **Air-gapped (E10 §4):** logging works (it is local); telemetry is forced off; no field that could carry warehouse content is ever emitted off-machine.
- **Determinism:** logging is a side effect off the deterministic compiler path; it never changes compiled SQL or a `QueryResult`. The accuracy harness is deterministic given fixed inputs and assertions.

---

## 10. Interfaces & touchpoints

- **E1:** owns `.canon/` (log location) and the `telemetry` config block; E16 writes under it.
- **E5/E7/E8 (serving):** emit `AnswerEvent` from the `QueryResult` metadata; `contract_schema` comes from the freeze (§4.4). No change to the frozen serving contract — logging reads metadata, adds nothing to the wire.
- **E4 (ingestion):** emits `reconcile_decision` events into the shared substrate (E4 §6).
- **E10 (LLM):** surfaces token/call/latency usage; E16 records it alongside `bytes_scanned` so LLM and warehouse cost sit in one log.
- **E13 (cost/cache):** populates `bytes_scanned` (P1, from E2), and the reserved `cache_hit`/`over_limit_blocked` when it lands.
- **E14 (trust):** populates the reserved `trust_score`; E16 supplies the inputs E14 weights and logs the result back.
- **E11 (feedback):** consumes `answer_outcome` events (§6) as evidence.

---

## 11. User stories & acceptance criteria

**S1 [P1] Every answer is logged, locally, without content.**
- AC1: When an answer is served, then an `AnswerEvent` is appended under `.canon/` with hashes for query and SQL, resolved bindings, guardrails fired, latency, and `bytes_scanned`.
- AC2: The log contains no result rows, no literal filter values, and no SQL text — only hashes and metadata.

**S2 [P1] The log is inspectable without telemetry.**
- AC1: Given served answers, when I run `canon status`/report, then I see counts, error distribution, latency, and `bytes_scanned` summaries, with telemetry still off.

**S3 [P1] Reserved fields don't break the schema.**
- AC1: Given E13/E14 not yet implemented, then `trust_score`/`cache_hit`/`over_limit_blocked` are present as null and the event validates; when those epics land, the same field is populated with no schema migration.

**S4 [P1] One substrate, two producers.**
- AC1: Both `served_answer` and `reconcile_decision` events are written to the same local log and are queryable together.

**S5 [P1] Air-gapped logging is content-safe.**
- AC1: Given `runtime.air_gapped: true`, then logging still works locally and telemetry cannot be enabled.

**S6 [L] Accuracy is tracked, not asserted.**
- AC1: Given a labeled question set and E15 assertions, when the harness runs, then it reports an accuracy number against a schema-only baseline.
- AC2: In CI, an accuracy regression returns a non-zero exit (`ASSERTION_FAILED`).

**S7 [L] Outcomes feed the metrics.**
- AC1: Given an analyst marks an answer incorrect and corrects it, then an `answer_outcome` event links to the original `AnswerEvent`, available to E11.

**S8 [L] Telemetry is opt-in and content-free.**
- AC1: Given `telemetry.enabled: false` (default), then nothing is sent.
- AC2: Given it is enabled, then only aggregate/anonymized fields per the documented schema are sent — never SQL, rows, or re-identifying hashes — with a clear opt-out.

---

## 12. Open questions (E16-specific)

- **Log rotation/retention:** size/age policy under `.canon/` as events accumulate; what (if anything) is summarized vs. discarded.
- **Query-hash re-identifiability:** confirm hashing the semantic query + SQL is sufficient to keep the log content-safe while still useful for correction-recurrence (which needs to match "the same query" across runs).
- **Baseline definition:** what exactly the "schema-only baseline" run is, so the accuracy lift is apples-to-apples.
- **Labeled set ownership:** where the benchmark question set lives and who curates it (overlaps with E15 assertions).
- **Telemetry schema review:** the precise aggregate fields and the disclosure/opt-out UX — needs a privacy review before any data is sent.
- **Outcome attribution:** correlating a downstream correction back to the exact served answer when an analyst edits SQL outside `canon`.

---

## 13. Out of scope (this spec)

- Trust-score computation and weighting (E14) — E16 logs the score and supplies inputs.
- Cost budgets, hard limits, and result caching (E13) — E16 logs their outcomes.
- Acting on outcomes / feeding reconciliation (E11) — E16 records outcomes only.
- Reconciliation decision logic (E4) — E16 provides the substrate.
- The compiler and serving surfaces (E5/E7/E8) — they emit events; E16 adds nothing to the frozen serving contract.
- LLM/warehouse runtime behavior (E10/E2) — E16 records the metrics they surface.
