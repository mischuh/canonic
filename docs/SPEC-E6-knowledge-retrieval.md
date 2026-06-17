# Spec — E6 Knowledge & Retrieval

**Status:** Draft for review
**Parent:** PRD — Context Layer for Data Agents (FR-5; §5.1 three surfaces, §5.2 layout; §9.1 Phase 1)
**Related:** SPEC E4 (auto-authoring + prune timing during ingest), SPEC E5+E15 (semantic-entity references, measure-fingerprint drift), SPEC E1 (`knowledge/`, `.canon/` index), SPEC E7+E8 (`search` capability surface). Extended later by E9 (interactive edit loop), E10 (local embeddings), E12 (multi-user access enforcement).
**Last updated:** 2026-06-15

E6 owns the second committed surface: free-form, searchable, auto-maintained **knowledge pages** that carry business meaning ("what does this mean to the business?"). It defines the page format, the reference graph that links pages to each other and to semantic entities, the rules that keep that graph valid, and the hybrid retrieval engine that serves it. The split rule (PRD §5.1): if a human needs it to *trust* an answer, it lives here; if it changes how SQL *runs*, it lives in semantics/contracts.

Phase markers: **[P1]** v1 core (E6 is a Phase-1 epic — this is its baseline), **[L]** later. E6 has no P0 deliverable.

---

## 1. Scope

In scope:
- The **knowledge page format**: Markdown body + frontmatter (`summary`, `tags`, `sl_refs`, `refs`, `usage_mode`).
- The **reference graph**: `sl_refs` (→ semantic entities), `refs`/`[[links]]` (→ pages); validation on write; pruning on ingest.
- **Scope & access**: global vs. user pages; the strict-additive collision rule (PRD FR-5).
- The **retrieval engine**: hybrid lexical + vector search with rank fusion, an embeddings-off fallback, scope/tag filters, and **graph traversal** that expands a hit's context without re-searching.
- **Drift & freshness**: rendering a measure's *live* definition rather than restating it; fingerprint-driven review flags; freshness surfaced at query time.
- `usage_mode` and its surfacing behavior.

Out of scope (own specs):
- **When** authoring/pruning happens in a run — E4 owns ingest timing and the propose-only diff; E6 provides the page template, validation, and prune *logic* it calls.
- Semantic-entity *definition* and the compiler — E5/E15. E6 references entities; it does not define or execute them.
- LLM prose generation and embedding *computation* — drafting prose is E4's builder; embedding model + runtime is E10. E6 consumes embeddings when present.
- The `search` capability's transport (CLI `knowledge search`, MCP `search_context`) — E7/E8. E6 implements what they call.
- Interactive page editing/review — E9 (Phase 2). E6's write-validation is reused there.
- Real multi-user access enforcement (RLS) — E12. v1 scope here is path-based (§4).
- Enforced caveats that become guardrails — those are contracts (E15), not knowledge. A knowledge page explains *why*; the contract makes SQL *obey* (PRD §5.1).

---

## 2. Knowledge page format [P1]

A page is Markdown with YAML frontmatter. Path determines scope and id.

```yaml
---
summary: "Why test accounts are excluded from active-customer counts."   # [P1] one line; indexed + shown in results
tags: [customers, definitions]                # [P1]
sl_refs:                                       # [P1] → semantic entities (E5), fully qualified
  - warehouse_pg.customers
  - warehouse_pg.orders.total_revenue
refs: [test-account-policy]                    # [P1] → other pages by id/slug
usage_mode: caveat                             # [P1] reference | caveat | policy | definition
meta:                                          # system-managed, not hand-edited
  provenance: human_curated                    # board_approved | human_curated | inferred  (matches E4/E5)
  last_validated_at: "2026-06-14T00:00:00Z"    # references last checked against live entities
  bound_fingerprints:                          # measure-definition fingerprints this page depends on (drift, §7)
    "warehouse_pg.orders.total_revenue": "sha256:…"
  frozen: false                                # [P1] human-owned; reconciliation flags but never edits (E4 §5.3)
---

Body in Markdown. Inline `[[test-account-policy]]` resolves to another page.
A measure is **referenced, never restated** — `{{ sl:warehouse_pg.orders.total_revenue.expr }}`
renders the current expr at read time, so it cannot drift out of sync (§7).
```

- **id/slug:** derived from filename; unique within scope.
- **`scope`** is derived from path (`knowledge/global/…` vs `knowledge/user/<id>/…`), not hand-set.
- **`frozen`** is the shared freeze marker E4 reads (E4 §5.3, §11). It lives in this file schema (file-schema version, not the frozen serving contract — SPEC P0-interface-freeze §6).

---

## 3. Reference graph [P1]

Pages form a navigable graph: `sl_refs` link to the semantic layer, `refs`/`[[links]]` link pages to each other.

### 3.1 Validation on write [P1]
Whenever a page is written — by E4's draft or a human edit (E9) — references are validated:
- Each `sl_ref` must resolve to a **live** semantic entity (source, column, dimension, or measure) via the E5/E15 entity index.
- Each `ref`/`[[link]]` must point at an existing page in a visible scope.
- A broken reference **blocks the write** with a precise location. You cannot author a page that points at nothing.

### 3.2 Pruning on ingest [P1]
When ingest (E4) detects that a referenced semantic entity has disappeared, every page whose `sl_refs` bound to it is affected. E6 supplies the prune logic; E4 emits the result as a **propose-only diff** (never a silent edit): the stale `sl_ref` is proposed for removal and the page's freshness is downgraded. Broken page-to-page `refs` are handled the same way. This is the "prune stale `sl_refs` during ingest" requirement (PRD FR-5), routed through E4's review flow.

---

## 4. Scope & access [P1]

Two scopes, path-defined (PRD §5.2): `knowledge/global/` (shared) and `knowledge/user/<id>/` (personal).

**Strict-additive rule (PRD FR-5, decided):** `knowledge/user/<id>` is **strictly additive**. A user page can *add* context but can **never** override or shadow a global one.
- On a name/topic collision, the **global page is authoritative**; the user page is surfaced as a **personal annotation attached to it**, never as a replacement.
- This keeps a single shared source of truth and matches the governance posture. Richer per-team overrides are **[L]** (deferred past v1, PRD §10).

**Access (v1):** path-based visibility — a user's search sees `global` + their own `user/<id>`, never another user's pages. This is *not* row-level security; real multi-user enforcement and a trust boundary are E12 (Phase 2). The current-user identity comes from runtime config; v1 assumes a cooperative single-tenant checkout.

---

## 5. Retrieval engine [P1]

Hybrid search over the committed pages, implemented on the project tech stack (tantivy BM25 + numpy vectors).

### 5.1 Two arms + fusion
- **Lexical [P1]:** a tantivy index over body + `summary` + `tags` → BM25 ranking. Always available.
- **Vector [P1, optional]:** page embeddings (from the optional local-embeddings runtime, E10) in a numpy store → cosine similarity. Present only when embeddings are installed.
- **Fusion [P1]:** when both arms run, combine by **Reciprocal Rank Fusion** (rank-based, so the two incomparable score scales need no normalization). Weighting is configurable; ties broken by a stable key (page id) so results are reproducible.

### 5.2 Embeddings-off fallback [P1]
When embeddings are disabled (the default install; PRD §10 open question), search runs **lexical-only** — BM25 plus `tags`/`summary` boosting — and degrades gracefully rather than failing. The same query returns sensible, if less semantically fuzzy, hits.

### 5.3 Filters & result shape
Search filters by scope (global + requesting user), `tags`, and `usage_mode`. The index lives under `.canon/` (local, rebuilt from committed Markdown; never committed) and is refreshed on ingest or page change.

```yaml
SearchResult:                       # P1 capability surface; new in P1, additive to the contract
  hits:
    - page: customers-active-definition
      scope: global
      score: 0.84
      summary: "…"
      matched_on: [lexical, vector]    # which arm(s) hit
      sl_refs: [...]
      annotations:                      # user pages attached to a global hit (§4 strict-additive)
        - { page: user/alice/customer-notes, scope: "user:alice" }
  traversed: [...]                      # graph-expanded pages (§6), if requested
```

This shape is a P1 capability (`search`/`search_context`), separate from the frozen P0 serving contract; introducing it follows the freeze's additive discipline (SPEC P0-interface-freeze §4.1) and does not change `query`/`compile`/errors.

---

## 6. Graph traversal [P1]

After search returns seed hits, the agent can pull connected context by **traversing the graph without re-searching** (PRD FR-5). From each seed, follow `sl_refs` / `refs` / `[[links]]` breadth-first up to a bounded depth, dedupe, and return the connected subgraph (pages + the semantic entities they bind). Bounded depth and a node cap prevent pulling the whole graph. Traversal is deterministic given the committed files.

This is what lets one search hit ("active customer") surface its bound caveats, the policy page it links to, and the semantic source it describes — as one coherent context bundle, in a single round trip.

---

## 7. Drift & freshness [P1]

**Live-definition rendering (drift resolution — PRD §10 / FR-13).** A page never copies a measure's SQL. It references the measure by name and the `{{ sl:…​.expr }}` directive renders the **live** `expr` at read time. The rendered definition therefore cannot fall out of sync with the semantic layer.

**Fingerprint review flag.** `meta.bound_fingerprints` records the measure-definition fingerprint each page depends on. When E15/E4 detect that a bound measure's `expr` changed, the page is **flagged for review** — because the surrounding *prose* (the "why") may no longer be accurate even though the rendered `expr` auto-updates. The flag is a review signal, not a silent edit; resolution flows through E4's diff/review.

**Freshness signal.** `meta.last_validated_at` records when a page's references were last checked against live entities. Serving (E7/E8) surfaces this as a staleness signal at query time (e.g. "referenced definitions unvalidated for 90 days") so the agent can caveat truthfully (PRD §5.1 freshness-as-first-class). Trust is not binary.

---

## 8. `usage_mode` & surfacing [P1]

`usage_mode` controls how a page participates in retrieval, beyond plain search:

| Mode | Behavior |
| --- | --- |
| `reference` | Found by search/traversal only. The default. |
| `caveat` | Auto-surfaced whenever a bound `sl_ref` entity appears in a result, so a relevant warning rides along even if the agent didn't search for it. |
| `policy` | A business rule/definition page; ranked and surfaced like `reference` but tagged so it is distinguishable in results. |
| `definition` | The canonical prose definition for a term; collisions across scope follow the strict-additive rule (§4). |

A `caveat` here *documents* a risk; if the risk must be *prevented*, it is promoted to a contract guardrail (E15) — knowledge explains, the contract enforces (PRD §5.1).

---

## 9. Interfaces & touchpoints

- **E4 (ingestion):** drives *when* pages are authored/pruned. E6 exposes the page template, write-validation (§3.1), and prune logic (§3.2); E4 supplies LLM-drafted prose and emits all changes as propose-only diffs.
- **E5/E15 (semantics/contracts):** the entity index that `sl_refs` validate against; the measure fingerprints that drive drift flags (§7). The `{{ sl:…​.expr }}` directive reads the live semantic definition.
- **E10 (embeddings):** optional local embedding runtime; when absent, §5.2 fallback. Embedding model choice and compute are E10's.
- **E7/E8 (serving):** implement `search`/`search_context` over E6; surface §7 freshness in the answer metadata.
- **E9 (edit loop, Phase 2):** reuses E6 write-validation for human/agent page edits.
- **E12 (access, Phase 2):** replaces §4's path-based visibility with enforced multi-user access.

---

## 10. Modes & determinism

- Search is **not** on the deterministic compiler path, so float-level variation in vector scores is acceptable. Determinism where it matters is preserved: the lexical arm, graph traversal, and tie-breaking (stable page-id key) are deterministic, so result *ordering* is reproducible for tests and the embeddings-off path is fully deterministic.
- In headless mode (no LLM), search still runs (retrieval needs no LLM); only authoring prose drafting is disabled, which lives in E4.

---

## 11. User stories & acceptance criteria

**S1 [P1] Author a valid knowledge page.**
- AC1: Given a page with `sl_refs` pointing at live semantic entities and `[[links]]` to existing pages, when it is written, then it passes validation and is indexed.
- AC2: Given a page with an `sl_ref` to a nonexistent entity, then the write is blocked with a precise location — never indexed broken.

**S2 [P1] Hybrid search returns relevant pages.**
- AC1: Given embeddings installed, when I search a business concept, then lexical and vector hits are fused (RRF) and `matched_on` records which arm(s) hit.
- AC2: Given embeddings disabled, then search runs lexical-only and still returns sensible hits — no failure.

**S3 [P1] Traverse the graph without re-searching.**
- AC1: Given a seed hit, when I expand, then its `sl_refs`, `refs`, and `[[links]]` are followed to a bounded depth and returned as one deduped subgraph, with no second search call.

**S4 [P1] User scope is strictly additive.**
- AC1: Given a global definition and a colliding `user/alice` page, when alice searches, then the global page is authoritative and her page is attached as a personal annotation — never a replacement.
- AC2: When bob searches, then alice's user page is never returned.

**S5 [P1] Stale references are pruned, not abandoned.**
- AC1: Given a semantic entity that disappeared at ingest, then each page bound to it gets a propose-only diff removing the stale `sl_ref` and a freshness downgrade — never a silent edit, never a dangling ref.

**S6 [P1] Definitions don't drift.**
- AC1: Given a page rendering `{{ sl:…​.expr }}`, when the measure `expr` changes, then the rendered definition reflects the new `expr` automatically.
- AC2: The page is flagged for prose review (bound-fingerprint mismatch) so the surrounding "why" can be re-checked.

**S7 [P1] Caveats ride along.**
- AC1: Given a `usage_mode: caveat` page bound to `total_revenue`, when a result references that measure, then the caveat is surfaced even though it was not directly searched for.

**S8 [P1] Freshness surfaces at query time.**
- AC1: When a page's references were last validated beyond the staleness window, then serving attaches a staleness signal the agent can caveat with.

**S9 [P1] Frozen page is never auto-edited.**
- AC1: Given `meta.frozen: true`, when conflicting ingest evidence arrives, then a contradiction is flagged and the page is untouched (E4 §5.3).

---

## 12. Open questions (E6-specific)

- **Fallback ranking quality** (PRD §10): how good lexical-only is without embeddings, and whether `summary`/`tags` boosting is enough — needs evaluation against a labeled set.
- **RRF weighting & traversal bounds:** default fusion weights, traversal depth, and node cap — tune against real graphs.
- **Chunking for vectors:** embed whole pages vs. sections, and how that interacts with `summary`-led ranking.
- **`caveat` surfacing volume:** auto-attaching caveats must not flood results; needs a relevance gate or cap.
- **Current-user identity in v1:** where path-scope identity comes from before E12 lands a real trust boundary.
- **Index freshness vs. cost:** incremental re-index on single-page change vs. full rebuild on ingest.
- **Drift-flag sensitivity** (shared with E15): which `expr` changes are review-worthy vs. cosmetic.

---

## 13. Out of scope (this spec)

- Ingest timing, the builder, and propose-only diff mechanics (E4).
- Semantic-entity definition, the compiler, and contract enforcement (E5/E15).
- Embedding model and runtime (E10); LLM prose generation (E4 builder).
- `search` transport and the daemon (E7/E8).
- Interactive editing/review workflow (E9).
- Real multi-user/RLS access enforcement (E12) — v1 is path-based visibility only.
