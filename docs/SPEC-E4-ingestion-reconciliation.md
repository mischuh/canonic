# Spec — E4 Ingestion & Reconciliation Engine

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-3; §5.3 ingestion flow, §5.6 operating modes; §9.1 Phase 1)
**Related:** SPEC E2 (normalized evidence input, schema-validation probe), SPEC E5+E15 (output file schemas + validation), SPEC E1 (`raw-sources/`, `.canon/`, setup bootstrap), SPEC E7+E8 (CLI `ingest`, headless runner). Extended later by E11 (answer feedback) and E3 (definition/evidence connectors).
**Last updated:** 2026-06-15

E4 is the engine that turns normalized source evidence into reviewable context. It reads what the connectors (E2/E3) emit, drafts proposed `semantics/` and `knowledge/` updates, **reconciles** them against already-accepted files under explicit provenance rules, and emits version-controlled diffs — never silent edits. It is the heart of the "auto-maintained context" thesis.

Phase markers: **[P1]** v1 core (E4 is a Phase-1 epic — this is its baseline), **[L]** later. E4 has no P0 deliverable; the thin tier-1 scaffold that `canon setup` may run in P0 (E1 §4) is superseded here by the real builder.

---

## 1. Scope

In scope:
- The **ingestion pipeline**: builder → reconciliation → validation → diff emission (PRD §5.3).
- The **context builder**: normalized evidence → proposed `semantics/*.yaml` + `knowledge/*.md` updates.
- The **reconciliation engine**: provenance tiers, confidence, freeze annotations, contradiction flagging, propose-only-by-default with a governed auto-apply threshold.
- **Diff emission** as reviewable, evidence-anchored changes; headless auto-PR role.
- **Snapshots + per-run event log** for an audit trail (every committed change traces to its evidence).
- **Re-runnable / idempotent** ingest and the fast initial bootstrap.
- The reconciliation data model and the evidence-input contract.

Out of scope (own specs):
- Connector internals and evidence *extraction* — E2/E3. E4 consumes **normalized evidence only**; no vendor shape reaches the engine.
- The output file *schemas* (`semantics/`, `contracts/`, `knowledge/` frontmatter) — E5/E15, E6. E4 writes to them; it does not define them. (These evolve under their own file-schema version, not the frozen serving contract — SPEC P0-interface-freeze §6.)
- The schema-validation **probe** against the live source — E2 §5. E4 *invokes* it for tier 4–6 evidence and consumes its result.
- Semantic-source validation rules — E5 §7. E4 *runs* them before emitting a diff.
- The compiler and serving (E5/E7/E8).
- Knowledge hybrid search/retrieval — E6 (E4 authors pages; E6 retrieves them).
- Answer-correctness outcomes as evidence — E11 (Phase 2). E4's evidence input is an open set so E11 plugs in without restructuring (§11).

---

## 2. Ingestion pipeline [P1]

Four ordered stages (PRD §5.3). The LLM (E10) is in the loop **only** in the builder and reconciliation *drafting* substeps; validation and apply are deterministic.

```text
normalized evidence (E2/E3)
      │
      ▼
1. Context builder      evidence → Proposal[]      (deterministic core + LLM-assisted drafting)
      │
      ▼
2. Reconciliation       Proposal[] × accepted files → ReconciliationReport (decisions, contradictions)
      │
      ▼
3. Validation           proposed output state → pass | VALIDATION_FAILED   (reuses E5 §7)
      │
      ▼
4. Diff emission        reviewable diffs + report   (propose-only; headless → auto-PR)
```

Nothing in stages 2–4 mutates accepted files in place. A change reaches a committed file only through a reviewed (or threshold-approved) diff.

---

## 3. Evidence input contract [P1]

E4 consumes a stream of **normalized evidence items**, each self-describing. This is the seam that keeps every source vendor out of the engine (PRD FR-2).

```yaml
EvidenceItem:
  source: warehouse_pg            # connection id (E1)
  kind: relation_schema           # relation_schema | observed_query | definition | doc_evidence
  acquisition_tier: live          # provenance of acquisition (E2 §4): live | declarative | … | hand_authored
  payload: { … }                  # one of E2 RelationSchema / ObservedQuery (E3 adds definition/evidence in-phase)
  source_fingerprint: "sha256:…"  # for drift/idempotency
  observed_at: "2026-06-14T…Z"
```

- `relation_schema` / `observed_query` come from E2 (primary connectors); `definition` / `doc_evidence` from E3 (definition/evidence connectors, landing in the same phase).
- The engine dispatches on `kind`, never on vendor. An evidence kind it doesn't handle is recorded and skipped, never guessed at.

---

## 4. Context builder [P1]

Turns evidence into **proposals**, not files.

- **Deterministic core (no LLM):** a `RelationSchema` maps directly to a `semantics/<conn>/<name>.yaml` draft — `table`, typed `columns`, `grain` candidate from the primary key, `joins` from discovered foreign keys, `meta.source_fingerprint`. This path is reproducible and is the *only* builder path in headless mode.
- **LLM-assisted drafting [P1]:** the fuzzy parts only — naming measures, drafting a knowledge page's prose, proposing a `grain` when no PK is declared, proposing joins from `observed_query` evidence. Each LLM-drafted proposal is labelled and carries a lower default confidence than a deterministic one.
- Output is a `Proposal[]`; no file is touched.

```yaml
Proposal:
  target: semantics/warehouse_pg/orders.yaml   # file to add/edit
  op: add | edit | prune                        # prune = target evidence disappeared
  content: { … }                                # the proposed YAML/MD fragment
  provenance: inferred                          # board_approved | human_curated | inferred (matches E5 meta)
  confidence: 0.82                              # builder's certainty in THIS inference (0–1)
  anchored_to: ["sha256:…"]                     # evidence fingerprints this proposal derives from
  drafted_by: deterministic | llm
```

`provenance` is **authority** (where it came from); `confidence` is **certainty** (how sure the builder is). They are independent axes: provenance governs overwrite priority (§5), confidence governs propose-vs-auto-apply and review ordering (§6).

New evidence always enters at provenance `inferred` — the lowest tier. Human edits and contract decisions sit higher and are never demoted by an ingest.

---

## 5. Reconciliation engine [P1]

Merges each proposal against the currently accepted file. The decision is deterministic given (existing fact, proposal, freeze state); the *drafting* upstream may have used an LLM, but the *decision* does not.

### 5.1 Provenance tiers (PRD FR-3)
`board_approved > human_curated > inferred`. Higher always wins. **Ingest never overwrites a higher tier with a lower one.** Since new evidence is `inferred`, it can never silently displace a curated or approved fact.

### 5.2 Decision table

| Existing fact | Proposal vs. existing | Outcome |
| --- | --- | --- |
| none | — | propose **add** |
| equal (fingerprint match) | — | no-op; refresh `last_validated_at` / fingerprint only |
| conflicts, existing tier **higher** | — | **flag contradiction**, keep existing, optionally record proposal as a deprecated alternative; never edit |
| conflicts, existing **frozen** (§5.3) | any tier | **flag contradiction**, never edit |
| conflicts, existing tier ≤ proposal, `confidence ≥ threshold` | — | propose **edit** (auto-apply only if policy allows §6) |
| conflicts, existing tier ≤ proposal, `confidence < threshold` | — | propose **edit**, marked low-confidence for review |
| target evidence disappeared | — | propose **prune** / mark stale (feeds freshness + ref pruning E6) |

A conflict is **never** resolved by silent overwrite. The worst case is a flagged contradiction a human resolves.

### 5.3 Freeze annotations (PRD FR-3)
A human-owned fact can be marked **frozen**. Reconciliation then *flags* conflicting evidence but **never edits** the fact, regardless of confidence or tier of the incoming evidence. (The `frozen` marker lives in the file schema owned by E5/E15/E6 — interface touchpoint, §11.)

### 5.4 Contradiction flagging (PRD FR-3)
Contradictions — new-vs-accepted, or two sources disagreeing in one run — are surfaced as structured report entries with both sides, their provenance, and a recommended action. They are **not** hard errors and do **not** fail a run by default; they ride into the review surface (a PR comment in headless mode) for a human to resolve. A strict mode that gates CI on contradictions is a configurable add-on (a MINOR-additive error code if introduced — does not touch the frozen serving contract).

### 5.5 Propose-only by default, with confidence (PRD FR-3)
Default behavior: **emit a diff, never auto-edit.** An explicit policy governs any auto-apply:

```yaml
reconcile:
  auto_apply:
    enabled: false                # default: propose-only
    min_confidence: 0.95          # only above this
    max_provenance: inferred      # never auto-apply over human_curated+
    never: [grain, joins, measures]   # structural fields always require review
```

Auto-apply is opt-in, bounded by confidence, capped at the lowest provenance, and forbidden for structurally risky fields.

---

## 6. Diff emission & audit [P1]

- **Output:** a set of reviewable diffs against committed files plus a `ReconciliationReport`. Every diff is **anchored to evidence** (`anchored_to`) so a reviewer can trace each change to its source (NFR reviewability).
- **Headless / auto-PR (PRD §5.6 role 1):** in pipeline mode the diffs are opened as an auto-PR against git, contradictions included as review notes. E4 produces the diff + report and may drive the PR step; it owns the *policy* (what to propose/auto-apply), not git itself — `canon` never owns the write-path (PRD non-goal).
- **Snapshots + event log (PRD FR-3 audit trail):**
  - *Scan snapshot* — the raw normalized evidence captured this run, written under `raw-sources/<connection-id>/` (committed [P1]) so the input is reproducible.
  - *Event log* — every reconciliation decision (add/edit/prune/contradiction/no-op) with its inputs, tier, confidence, and anchored evidence, in `.canon/` (local). Together they make every committed change traceable to the evidence and decision that produced it.

---

## 7. Re-runnable & idempotent ingest [P1]

- Re-running `canon ingest` refreshes from all configured sources.
- **Idempotency by fingerprint:** if `source_fingerprint` is unchanged, the item is a no-op (only `last_validated_at` refreshes). A run with no upstream change proposes **no** diffs.
- Drift is detected as a fingerprint change → a normal `edit`/`prune` proposal through reconciliation.

---

## 8. Fast initial bootstrap [P1]

The setup-time path (PRD FR-3; invoked from E1 §4 setup):
- Runs **tier-1 live introspection** (E2) for the first connection and drafts semantic sources deterministically — enough to make the agent useful on day one without a full multi-source reconcile.
- Supersedes the optional thin scaffold E1 may run in P0. Knowledge-page drafting and cross-source reconciliation are part of the full ingest, not the bootstrap.

---

## 9. Operating modes [P1]

| | Interactive / agent mode | Headless / pipeline mode |
| --- | --- | --- |
| LLM in loop | Yes — builder + reconciliation drafting | No (or off the critical path) |
| Builder path | deterministic core + LLM drafts | **deterministic core only** |
| Output | proposed diffs for review | auto-PR + reconciliation report, exit codes |
| Determinism | not required | **required** — identical evidence + accepted state → identical proposals & decisions |

Headless determinism holds because the reconciliation *decision* is deterministic; only the optional LLM *drafting* is non-deterministic, and it is disabled on the critical path. This is what makes scheduled ingest (PRD §5.6 role 1) a safe, repeatable job.

---

## 10. Validation before emit [P1]

Before any diff is emitted, the proposed output state is validated:
- **Schema-validation probe (E2 §5):** for evidence acquired via ladder tiers 4–6, the live-source probe must pass; a `SCHEMA_MISMATCH` becomes a contradiction/validation failure, never silent acceptance.
- **Semantic/contract validation (E5 §7):** reference integrity, types, grain, cross-surface references on the *proposed* files. A diff that would produce invalid context fails with `VALIDATION_FAILED` and is not emitted.

Both reuse existing codes from the canonical registry (SPEC E7/E8 §6.1); E4 introduces no breaking change to the frozen serving contract.

---

## 11. Interfaces & touchpoints

- **Input:** normalized `EvidenceItem` stream (§3) from E2 now, E3 in-phase.
- **Output:** diffs against E5/E15/E6 file schemas; validation via E5 §7; probe via E2 §5.
- **Freeze marker:** a `frozen` annotation on a fact — E4 reads it, the field lives in the E5/E15/E6 file schema (file-schema version, not `contract_schema`). **Open touchpoint** to confirm with those specs.
- **E11 (Phase 2):** answer-correctness outcomes are just another `EvidenceItem.kind`. Keeping `kind` an open set and provenance/confidence on every proposal means E11 plugs into reconciliation without restructuring. In scope: don't preclude it. Out of scope: implement it.

---

## 12. User stories & acceptance criteria

**S1 [P1] Build semantics from live evidence.**
- AC1: Given `RelationSchema` evidence for a table, when I ingest, then a `semantics/<conn>/<name>.yaml` proposal is produced deterministically with table, typed columns, PK-derived grain, and FK-derived joins.
- AC2: A proposal with no PK draws an LLM-drafted `grain` candidate, labelled `drafted_by: llm` with reduced confidence — never a silently asserted grain.

**S2 [P1] Provenance protects curated facts.**
- AC1: Given a `human_curated` measure definition and conflicting `inferred` evidence, when I ingest, then the curated fact is kept, the conflict is flagged, and no edit is proposed to it.

**S3 [P1] Frozen facts are never edited.**
- AC1: Given a fact marked frozen, when conflicting evidence of any tier/confidence arrives, then a contradiction is flagged and the fact is untouched.

**S4 [P1] Contradictions surface, never overwrite.**
- AC1: Given two sources disagreeing on a column type in one run, then the run completes, both sides appear in the reconciliation report with provenance and a recommended action, and neither silently wins.

**S5 [P1] Propose-only by default; bounded auto-apply.**
- AC1: With default config, ingest emits diffs and edits no committed file in place.
- AC2: With `auto_apply.enabled` and `min_confidence: 0.95`, only `inferred`-tier, ≥0.95-confidence, non-structural proposals are applied; a `grain` change is still proposed for review regardless of confidence.

**S6 [P1] Idempotent re-run.**
- AC1: Given no upstream change since the last run, when I re-ingest, then zero diffs are proposed and only `last_validated_at` is refreshed.
- AC2: Given a changed `source_fingerprint`, then exactly the affected proposal is produced.

**S7 [P1] Audit trail.**
- AC1: After a run, the raw evidence snapshot exists under `raw-sources/<conn>/` and every emitted diff is anchored to the evidence fingerprints that produced it.
- AC2: The event log records each decision with its tier, confidence, and inputs.

**S8 [P1] Validation gates emission.**
- AC1: Given a proposal that would break reference integrity, then `VALIDATION_FAILED` is raised with a precise location and the diff is not emitted.
- AC2: Given declarative/hand-authored evidence (tier 4–6) that fails the live probe, then `SCHEMA_MISMATCH` is surfaced and the evidence is not committed.

**S9 [P1] Headless determinism & auto-PR.**
- AC1: Given identical evidence and accepted state, two headless runs produce byte-identical proposals and decisions.
- AC2: In headless mode the run opens an auto-PR carrying the diffs and contradiction notes, and returns a clean exit on success.

**S10 [P1] Disappeared evidence is pruned, not abandoned.**
- AC1: Given a previously-ingested relation that no longer exists at source, then a `prune`/stale proposal is produced and the freshness signal is updated — never a dangling reference.

---

## 13. Open questions (E4-specific)

- **Auto-apply threshold calibration** (PRD §10): the default `min_confidence` and the structural-field denylist need tuning against real projects; ships conservative (propose-only).
- **Contradiction in CI:** default is report-and-PR (non-failing); confirm whether a strict gate-on-contradiction mode is needed in v1 and, if so, its (additive) error code.
- **Confidence calibration:** how the builder assigns `confidence`, and whether deterministic-vs-LLM origin alone is a sufficient first-pass proxy.
- **Grain inference without a PK:** LLM-drafted grain vs. declining and requiring a human — how aggressive to be.
- **Frozen-annotation home:** confirm the `frozen` marker's field and location with E5/E15/E6 (file-schema touchpoint, §11).
- **Snapshot retention:** size/retention policy for `raw-sources/` as runs accumulate; what to commit vs. keep local.
- **Canonical-binding bootstrap** (PRD §10): how initial `inferred` evidence proposes E15 metric bindings without overwhelming review — likely a separate low-volume proposal stream gated harder than semantic drafts.
- **E3 evidence ordering:** when both primary (E2) and definition/evidence (E3) sources are present, the reconcile order and whether modeling-code definitions (ladder tier 2) outrank raw introspection by default.

---

## 14. Out of scope (this spec)

- Evidence extraction and connector internals (E2/E3).
- File schemas for `semantics/`/`contracts/`/`knowledge/` (E5/E15, E6).
- Knowledge retrieval/search (E6).
- The compiler and serving surfaces (E5/E7/E8).
- Answer-feedback evidence (E11) — interface accommodated, not implemented.
- Git/PR mechanics beyond emitting the diff and driving the documented auto-PR step; `canon` owns no write-path or orchestration (PRD non-goal).
