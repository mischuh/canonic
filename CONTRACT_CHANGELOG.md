# Contract changelog

This file is a manually maintained record of every change to
`CONTRACT_SCHEMA` (`canonic/contract.py`), the single source of truth for
`contract_schema` across the semantic query, `QueryResult`/compile output,
error registry, and `ContractResolver` hook surfaces frozen by
`docs/SPEC-P0-interface-freeze.md`.

It is **never** updated by release automation. Per
`docs/SPEC-P0-interface-freeze.md` §7, every `CONTRACT_SCHEMA` change must:

1. Have an ADR/RFC classifying it as MINOR or MAJOR per §4.
2. Update the affected source spec(s) and golden schema snapshot(s) in the
   same PR.
3. Add an entry below in the same PR that bumps `CONTRACT_SCHEMA`.
4. Cite the ADR/PR that performed the classification.

CI (`.github/workflows/contract-schema-guard.yml`,
`scripts/check_contract_changelog.py`) fails any PR that changes
`CONTRACT_SCHEMA` without a corresponding entry here.

## Format

```
## <new_version> (<date>) — MINOR|MAJOR

- ADR/PR: <link>
- Summary: <what changed and why>
```

## History

## 2.6 (2026-08-13) - MINOR

- ADR/PR: this PR (feat(compiler): compose several metrics into one compiled query)
- Summary: A request for more than one metric now compiles to a single SQL statement
  for every binding kind. Previously only plain additive metrics could be combined, and
  `ratio`/`weighted_avg`, `semi_additive`, `distinct_count`/`percentile` and `opaque`
  each raised `unsupported_measure` ("must be queried alone") whenever a second metric
  was requested. Each metric, and each component of a composite, is now planned as its
  own leaf aggregating to the requested dimensions, and one outer SELECT assembles them
  over a shared grain spine (AMENDMENT-multi-metric-compose).

  No shape change and no new error codes: `metrics` was already a list in the v1 frozen
  field set, and `unreachable`, `unresolved`, `ambiguous`, `unsupported_measure` and
  `fanout_unsafe` cover every new failure path. Classified MINOR under §4.1 because a
  request that previously failed now succeeds, which is behaviour a client may reasonably
  want to negotiate on before sending multi-metric requests (§4.4); without a bump,
  multi-metric support is undetectable except by trying it.

  Behaviour that changes for callers, all of it in the direction of refusing to guess:
  - A metric with no rows for a dimension value another metric does have now reports
    NULL rather than a measured `0`. Conditional aggregation used to report zero, which
    claims a measurement that was never made.
  - Stage 1 reports *every* unresolved or ambiguous metric in one error instead of the
    first, so a caller fixes them in one round trip. `unresolved` takes precedence over
    `ambiguous` when both occur.
  - `UNREACHABLE` messages now name the leaf that could not bind the dimension or filter.
  - `related.unused_dimensions` only suggests dimensions addable from *every* queried
    source, so a suggestion can no longer lead into an `unreachable` on the next request.
  - Two metrics on unrelated sources, and a non-additive metric beside a metric on a
    fanning source, now compile instead of raising: each is aggregated at its own grain,
    so there is no join left to be unsafe.

## 2.5 (2026-08-01) - MINOR

- ADR/PR: this PR (feat(instrumentation): add opt-in telemetry transport)
- Summary: Add `telemetry_not_configured` (exit 20) and `telemetry_send_failed`
  (exit 21) to the canonical error registry, for `canonic report --telemetry-send`
  (SPEC-E16 §8/§12). Additive: existing codes and exits are unchanged. Sending
  remains gated behind `telemetry.enabled`, `telemetry.endpoint`, and
  `telemetry.transport_acknowledged` all being explicitly set in `canonic.yaml`,
  so `canonic report --telemetry-preview`'s output and content-safety guarantees
  are unaffected.

## 2.4 (2026-07-30) - MINOR

- ADR/PR: this PR (feat(compiler): enforce required_dimension guardrails and honor severity)
- Summary: `required_dimension` guardrails (previously declared and reported in
  `guardrails_fired` but never enforced) now block or warn when their dimension is
  neither grouped by nor filtered on. `severity: warn` now actually downgrades
  `min_trust`, `restrict_source`, and `required_dimension` guardrail blocks to a
  `warnings[]` entry instead of raising `GUARDRAIL_BLOCK`; `mandatory_filter` still
  always injects its predicate but now also warns at `severity: warn`. Additive:
  `FiredGuardrail`/`FiredGuardrailOut` gain a `severity` field (defaults to `"error"`),
  and the `Guardrail` contract model gains a `dimension` field (required only for
  `required_dimension`). No existing consumer's shape changes.

## 2.3 (2026-07-11) - MINOR

- ADR/PR: specs/AMENDMENT-remote-mcp-transport.md (feat(mcp): remote http transport with bearer-token auth)
- Summary: Add `user` (verified bearer-token client_id) to `AnswerEvent`, populated
  for MCP `http`-transport `query`/`run_sql` calls so per-user attribution flows into
  the event log. Additive field, `None` for stdio/CLI callers; no existing consumer's
  shape changes.

## 2.2 (2026-07-09) - MINOR

- ADR/PR: bced9e6 (feat(instrumentation): add E16 Part 2 full instrumentation)
- Summary: Log the E14 trust tier on every `AnswerEvent`. Additive field on
  the instrumentation payload; no existing consumer's shape changes.

## 2.1 (2026-07-08) - MINOR

- ADR/PR: 5c42da2 (feat(trust): add E14 answer trust score and min_trust guardrail)
- Summary: Add a per-answer trust tier (trusted/provisional/caution) with
  inspectable reasons to `QueryMetadata`, plus a new `min_trust` guardrail
  kind. Additive to the existing contract surface.

<!-- Add new entries above this line, most recent first. -->
