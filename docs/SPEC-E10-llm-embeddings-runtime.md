# Spec — E10 LLM & Embeddings Runtime

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-8; §5.6 operating modes, §7 NFR data-residency; §9.1 Phase 1)
**Related:** SPEC E1 (`llm`/`embeddings` config schema + secret indirection, offline install), SPEC E4 (generation call sites: drafting + reconciliation), SPEC E6 (embeddings consumer: vector search arm), SPEC E16 (LLM usage metrics). Cost note: E13 (warehouse cost is separate).
**Last updated:** 2026-06-15

E10 is the runtime that turns the configured `llm`/`embeddings` blocks into actual model calls. It gives the rest of the system one interface to generation and one to embeddings, regardless of provider, and it owns the **offline/air-gapped guarantee** — the privacy differentiator that no warehouse content or context ever leaves the machine. E1 pins the *config shape*; E10 owns the *behavior* behind it (E1 §3: "SPEC scope: E10 owns details").

Phase markers: **[P1]** v1 core (E10 is a Phase-1 epic — this is its baseline), **[L]** later. The `canon.yaml` config fields are **[P0]** (reserved early by E1); the runtime that uses them is [P1] — the Phase-0 compile/query path needs no LLM.

---

## 1. Scope

In scope:
- **Provider abstraction** over litellm: one generation interface, many backends; OpenAI-compatible `base_url` covers local + hosted with no engine-specific code.
- **Task-based model routing**: per-task model selection (cheap/local for drafting, stronger for reconciliation).
- **Offline / air-gapped mode**: an *enforced* configuration where every model endpoint is local/private and egress is blocked — defense-in-depth, not documentation.
- **Local embeddings runtime** (sentence-transformers): optional installable add-on; model-identity signal for index compatibility.
- **BYO keys** via E1 secret indirection.
- A **tested local-model baseline** for the LLM-in-loop tasks.
- The **runtime interfaces** E4 and E6 call, and the determinism boundary.

Out of scope (own specs):
- The `llm`/`embeddings` *config schema* and `*_ref` secret indirection — E1 §3/§7. E10 consumes them.
- **When** generation runs (builder/reconcile substeps) — E4 §4. E10 only resolves task → model and executes.
- **When/how** embeddings are used in search (vector arm, index, fusion) — E6 §5. E10 only produces vectors.
- The deterministic compiler — E5; it is LLM-free by design and E10 is never on its path (§9).
- Warehouse query cost control (bytes/rows/budgets) — E13. That is a different axis from LLM token cost (§10).
- Offline *install* (no outbound during install) — E1 §5. E10 owns the offline *runtime*; the two together deliver air-gapped operation.

---

## 2. Provider abstraction [P1]

One generation interface, implemented over litellm, so adding or swapping a backend is config, not code.

- **`provider: openai_compatible`** (the E1 default) plus `base_url` covers **both** local runtimes (Ollama, vLLM, LM Studio, llama.cpp, text-generation-inference) **and** hosted OpenAI-compatible endpoints — they differ only in `base_url` and whether a key is needed. "Docks in without engine-specific code" (PRD FR-8) is the requirement: no per-engine branch in core logic.
- Other litellm-supported providers are reachable through the same interface where a user configures them, but `openai_compatible` is the first-class, tested path.
- **Structured output [P1]:** generation can request a JSON-schema-constrained response so E4 receives parseable proposals, not prose to scrape. Models that cannot honor the schema fail with a clear error rather than returning unparseable text (baseline caveat, §7).

---

## 3. Task-based model routing [P1]

Different work wants different models (PRD FR-8, §5.6): cheap/local for high-volume drafting, stronger for the judgment-heavy reconcile step. E10 resolves a **named task** to a model config.

```yaml
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: <default-model>                 # used when a task has no override
  api_key_ref: env:CANON_LLM_KEY         # resolved at call time (E1 indirection); nullable for local
  tasks:                                  # [P1] per-task overrides
    draft:     <small-local-model>        # E4 builder: measure naming, grain/joins, knowledge prose
    reconcile: <stronger-model>           # E4 reconciliation: conflict resolution drafting
```

- **Tasks (v1):** `draft` (E4 builder) and `reconcile` (E4 reconciliation). A task with no override uses the default `model`.
- Resolution is deterministic: task → override or default. No silent model substitution — a failed call surfaces a structured error after bounded retries, it does **not** quietly fall back to a different model (which would change behavior invisibly).
- Per-invocation override and the flag-vs-config precedence rule are E1's (E1 §9 open question); E10 honors whatever resolved config it is handed.

---

## 4. Offline / air-gapped mode [P1] — the differentiator

A fully-local config (local LLM `base_url` + local embeddings) must support operation where **no warehouse content or context ever leaves the machine/network** (PRD FR-8, §7 data-residency). This is a privacy guarantee, so — like read-only for the database (E2 §3) — it is **enforced, defense-in-depth**, not merely possible.

```yaml
runtime:
  air_gapped: true        # [P1] when set, egress to non-local endpoints is refused
```

When `air_gapped: true`:
1. **Load-time validation:** every configured endpoint (`llm.base_url`, embeddings) must resolve to a local/private address (localhost or a private/LAN range on an explicit allowlist). A public endpoint in config is a hard error at load — the daemon does not start mis-configured.
2. **Call-time enforcement:** any attempted model call to a non-allowlisted host is blocked before the request leaves the process, aborting with a clear `AIR_GAPPED_VIOLATION`-style error. A misconfiguration cannot silently exfiltrate context.
3. **No telemetry, no remote keys:** opt-in telemetry (E16) is incompatible with air-gapped and is forced off; a `*_ref` pointing at a remote secret service is rejected.

This pairs with E1's offline *install* path (no outbound during install): install offline, run air-gapped, and warehouse content stays on-machine end to end.

---

## 5. Local embeddings runtime [P1, optional]

Powers E6's vector search arm. Implemented on sentence-transformers.

```yaml
embeddings:
  provider: local
  model: <embedding-model>
```

- **Optional add-on install** (E1 §5: not bundled by default). When not installed, E10 reports embeddings unavailable and E6 runs lexical-only (E6 §5.2) — graceful degradation, never a failure.
- **Interface:** `embed(texts) -> vectors`, plus `is_available()` and a **model identity/fingerprint**.
- **Index compatibility [P1]:** the embedding model's identity is exposed so E6 can detect a model change — vectors from a different model are incompatible, so a model change must trigger a **reindex** (E6 owns the index; E10 supplies the signal). Mixing vectors from two models silently is forbidden.
- Embeddings run locally and so are inherently air-gap-compatible; a hosted embedding provider is allowed only outside air-gapped mode.

---

## 6. Secrets & BYO keys [P1]

- Keys are **bring-your-own** and never literal in `canon.yaml`: `api_key_ref` points at env / OS-keyring / `.canon/` file (E1 §3/§7 indirection). E10 resolves the ref **at call time**, never logs the value, and never writes it to the event log.
- Local endpoints typically need no key (`api_key_ref` nullable).
- A literal-looking secret in config is already rejected by E1 validation; E10 additionally refuses to proceed if a required key ref resolves to nothing.

---

## 7. Tested local-model baseline [P1]

A published, versioned baseline so self-hosters know what actually works (PRD FR-8). It documents, for the LLM-in-loop tasks (`draft`, `reconcile`):
- Which local models reach acceptable quality, measured by the E16 accuracy harness on a labeled drafting/reconciliation set — **not** "compilation quality" in the literal sense, since the compiler (E5) is deterministic and LLM-free; the baseline measures the *drafting that feeds* compilable semantics and the *reconciliation judgment*.
- Each model's ability to honor structured (JSON-schema) output, since smaller local models vary here (§2).
- Recommended task→model pairings (a small local model for `draft`, a stronger one for `reconcile`).
- Published per release and re-runnable, so the baseline tracks reality as models churn rather than being asserted once.

---

## 8. Runtime interfaces

What E10 exposes to its two consumers; everything else is internal.

```text
GenerationRuntime (consumed by E4):
  generate(task, messages, *, response_schema?) -> Completion   # task → resolved model (§3)
  # Completion carries text|structured payload + usage (tokens/calls) for E16

EmbeddingRuntime (consumed by E6):
  embed(texts) -> vectors
  is_available() -> bool
  model_identity() -> fingerprint          # drives E6 reindex on change (§5)
```

- Generation returns **usage metrics** (token/call counts, latency) so E16 can log LLM cost alongside warehouse `bytes_scanned`; E10 surfaces, E16 records (§10).
- Errors are structured (provider error, timeout-after-retries, schema-violation, air-gapped block), never prose-only, so callers act programmatically.

---

## 9. Modes & determinism

| Mode | Generation | Embeddings |
| --- | --- | --- |
| Interactive / agent | on (`draft`, `reconcile`) | on if installed |
| Headless / pipeline | **off** (deterministic path; E4 §9) | optional — vector search still works; ingest drafting disabled |
| Air-gapped | local only, egress blocked (§4) | local only |

- **E10 is never on the deterministic compiler path.** Compile/query (E5) is LLM-free by construction; E10 generation is used only in E4 drafting and is disabled in headless mode. This is what keeps the frozen serving contract reproducible (SPEC P0-interface-freeze).
- Vector search is not on the deterministic path either; E6's lexical arm and traversal stay deterministic, and the embeddings-off path is fully deterministic — so "no models configured at all" is a valid, fully-deterministic operating point (headless ingest + lexical search).

---

## 10. Interfaces & touchpoints

- **E1:** owns the `llm`/`embeddings`/`runtime` config schema and secret indirection; E10 consumes resolved config. Flag-vs-config precedence is E1 §9.
- **E4:** the only generation consumer — calls `generate(task, …)` for builder drafting and reconciliation; supplies `response_schema` for parseable proposals.
- **E6:** the only embeddings consumer — calls `embed`/`is_available`/`model_identity`; reacts to a model-identity change with a reindex.
- **E16:** logs the usage metrics E10 surfaces (LLM tokens/calls/latency) into the local event log; air-gapped forces telemetry off (§4).
- **E13:** warehouse cost control is a *separate* axis (bytes/rows/budgets on SQL execution). LLM token cost is surfaced by E10 for visibility; enforcing an LLM spend budget, if wanted, is a later concern, not E13's warehouse scope.

---

## 11. User stories & acceptance criteria

**S1 [P1] One interface, any backend.**
- AC1: Given `provider: openai_compatible` with a local `base_url`, when E4 requests a draft, then the call succeeds with no engine-specific code path.
- AC2: Given the same config repointed at a hosted OpenAI-compatible `base_url`, then only `base_url`/key change — no core code change.

**S2 [P1] Per-task routing.**
- AC1: Given `llm.tasks.reconcile` set, then reconciliation uses that model while `draft` uses the smaller default — verified by the resolved model per task.
- AC2: A task with no override resolves to the default `model`.

**S3 [P1] Air-gapped is enforced, not advisory.**
- AC1: Given `runtime.air_gapped: true` and a public `llm.base_url`, then the daemon fails to start with a clear error — no mis-configured run.
- AC2: Given air-gapped with valid local endpoints, when any code attempts a call to a non-allowlisted host, then it is blocked before egress and aborts with a clear error.
- AC3: Given air-gapped, then telemetry is forced off and a remote secret-service `*_ref` is rejected.

**S4 [P1] Embeddings optional, degrades gracefully.**
- AC1: Given embeddings not installed, then `is_available()` is false and E6 runs lexical-only without failing.
- AC2: Given the embedding model identity changes, then E10 reports the change so E6 triggers a reindex; vectors from two models are never mixed.

**S5 [P1] Keys via indirection only.**
- AC1: Given `api_key_ref: env:…`, then the key is resolved at call time, never logged, never written to the event log.
- AC2: Given a required key ref that resolves to nothing, then the call fails with a clear error.

**S6 [P1] No silent model substitution.**
- AC1: Given a model call that fails after bounded retries, then a structured error is surfaced — E10 does not quietly switch to a different model.

**S7 [P1] Deterministic without models.**
- AC1: Given headless mode with generation off, then ingest runs its deterministic builder core and search runs lexical-only — a complete, reproducible run with no model calls.

**S8 [P1] Usage is observable.**
- AC1: Each generation returns token/call/latency usage that E16 logs alongside warehouse bytes scanned.

**S9 [P1] Baseline is real.**
- AC1: The tested-baseline doc lists, per task, models that pass the E16 accuracy harness and their structured-output behavior, and is published per release.

---

## 12. Open questions (E10-specific)

- **"Local" detection for air-gapped:** the exact allowlist rule — localhost only, or localhost + configurable private/LAN CIDRs for a separate on-prem inference host? Must be tight enough to be a real guarantee.
- **Structured-output reliability on small local models:** how strictly to require JSON-schema adherence vs. a tolerant parse-and-repair step, and how that interacts with the baseline.
- **Embedding-model migration:** auto-reindex on identity change vs. require an explicit `canon` command (interacts with E6 index cost).
- **Tested baseline maintenance:** which models to include and the cadence for refreshing as local models churn.
- **Retry/timeout defaults** per task, and whether they are configurable.
- **LLM cost metering depth:** how much E10 surfaces beyond raw usage, and whether an LLM spend budget belongs anywhere in v1.
- **Task taxonomy:** are `draft` and `reconcile` sufficient, or will knowledge authoring / canonical-binding bootstrap want their own task slots (interacts with E4 §13)?

---

## 13. Out of scope (this spec)

- The `llm`/`embeddings`/`runtime` config schema and secret indirection (E1).
- Generation call timing and the ingest pipeline (E4).
- Vector index, fusion, and search behavior (E6).
- The deterministic compiler (E5) — LLM-free; E10 is never on its path.
- Warehouse query cost/budgets (E13).
- Offline *install* mechanics (E1) — E10 owns the offline *runtime*.
- Telemetry transport and the accuracy harness internals (E16) — E10 supplies inputs.
