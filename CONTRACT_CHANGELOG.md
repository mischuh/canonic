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

## Recorded without a bump

A change listed here was reviewed against the classification rules and did not bump
`CONTRACT_SCHEMA`. Recording it keeps the decision findable.

### `dimensions[].expr` (0.31.0) and `dimensions[].json_path` (0.32.0)

- ADR/PR: AMENDMENT-dimension-expr §6, AMENDMENT-json-path-dimension §6 (feat(semantic) #357 for
  `json_path`)
- Decision: no bump. Both are optional fields on the semantic-source file schema
  (`semantics/*.yaml`), which feeds the compiler. `contract_schema` freezes the semantic query,
  the `QueryResult` and compile output, the error registry and the `ContractResolver` hooks,
  and none of those changed shape. A file that used to fail validation now compiles, which is
  not a change a client reads off the wire.
- Why not the MINOR bump both amendments recommend: the releases already shipped, so a bump
  now would sit on an unrelated change, and a version number that appears only after the fact
  cannot tell a client whether an older server supports derived dimensions. A client that
  needs to know reads `canonic_version`.

### Open-period warning and `dimensions[].time_grain` (feat(trust) #417)

- ADR/PR: this PR (feat(trust): flag results that include a period that has not ended)
- Decision: no bump. `time_grain` is an optional field on the semantic-source file schema and
  `trust.flag_open_periods` an optional key of `canonic.yaml`. Neither changes the shape of the
  semantic query, `QueryResult`, the compile output, the error registry or the `ContractResolver`
  hooks. The check adds an entry to the existing `warnings` list and a reason to the existing
  `trust_score.reasons`, both lists of free text, and the tier it can set (`provisional`) is
  already part of the contract. A client that renders `warnings` and `reasons` needs no change.

### Open-period check for filter-only queries (feat(trust) #427)

- ADR/PR: this PR (feat(trust): flag filter-only queries that include a period that has not ended)
- Decision: no bump. The check extends the open-period warning to a query that only filters on a
  time dimension. The response shape does not change: it adds an entry to `warnings` and a reason
  to `trust_score.reasons`, as before. The compiled SQL of such a query gains one column,
  `_canonic_latest_period_<dimension>`, which `query` strips from the result, the same mechanism
  as the zero-denominator column recorded under 3.6. A consumer that executes `compiled.sql`
  itself sees the extra column and can ignore it. For a time dimension on a joined source the
  column is named `_canonic_latest_period_<alias>_<dimension>` (feat(trust) #430).

### Zero-denominator warning follows the count (feat(compiler) #433)

- ADR/PR: this PR (feat(compiler): warn about a zero denominator only when a returned row has one)
- Decision: no bump. `warnings` is a list of free text and keeps its shape. In `query`, the warning of a
  ratio under `on_zero_denominator: null` now appears only when `zero_denominator_rows` is above zero, and
  then reads `zero denominator in <n> rows for metric '<metric>' yields NULL (on_zero_denominator=null)`. The
  `compile` output has no count and keeps the generic line. A client that matches the old text exactly needs
  to match the new one as well.

### Reload on project file changes (fix(core) #434)

- ADR/PR: this PR (fix(core): reload contracts and semantics when project files change)
- Decision: no bump. A running server now serves the definitions on disk instead of the ones it
  loaded at startup. No field, type or error code changes. When an edit does not validate, the
  previous definitions keep serving and `warnings` of `query` and `compile_query` gains one entry
  of free text that names the failing file. Reloading is off by default since the
  `project.reload` setting (feat(core) #440), see 3.8.

## History

## 3.9 (2026-10-19) - MINOR

- ADR/PR: this PR (feat(mcp): return the knowledge pages that define a metric in describe_metric)
- Summary: `describe_metric` and each entry of `list_metrics` gain `knowledge_refs`: the ids of the
  visible knowledge pages that define or qualify the metric, sorted. They come from the first
  step that applies: the optional `knowledge_refs` on the metric binding, the pages that name the
  metric in the new `metrics` front matter list, then the definition and caveat pages whose
  `sl_refs` overlap the entities of the metric's filter. The binding and the knowledge page schema
  gain those optional fields. `[]` when nothing applies. A consumer that ignores unknown fields is
  unaffected.

## 3.8 (2026-10-18) - MINOR

- ADR/PR: this PR (feat(core): make reloading project files opt-in and report drift when it is off)
- Summary: `contract_info` gains `project_reload` (`off` or `auto`) and `project_files_changed`.
  The latter is `true` when the contents of `canonic.yaml`, `contracts/` or `semantics/` differ
  from the state the server is serving, which is how a server with `project.reload: off` shows
  that its files moved on. Both are present for every server, `off` and `false` for a service
  built in memory. A consumer that ignores unknown fields is unaffected. The behaviour of #434
  is now opt-in through `project.reload: auto` in `canonic.yaml`, or `--reload` on
  `canonic mcp start`.

## 3.7 (2026-10-18) - MINOR

- ADR/PR: this PR (feat(mcp): expose a fingerprint of the loaded project state)
- Summary: `contract_info` gains `project_fingerprint` and `project_loaded_at`. The fingerprint is a
  hash over the contents of `canonic.yaml`, `contracts/` and `semantics/`, so the same project
  yields the same value on every machine. `QueryMetadata`, which the query and compile outputs
  share, and `AnswerEvent` gain an optional `project_fingerprint`, so an answer can be traced to
  the definitions that produced it. All fields are `null` for a service built in memory. A
  consumer that ignores unknown fields is unaffected.

## 3.6 (2026-10-18) - MINOR

- ADR/PR: this PR (feat(compiler): count zero-denominator substitutions in the query result)
- Summary: Each entry of `metadata.composition` gains an optional `zero_denominator_rows`: how many
  rows of the returned result had a zero denominator and were given the value of
  `on_zero_denominator` (`NULL` or `0`). It is `null` when nothing was counted: in the compile
  output, which has no result, and under `on_zero_denominator: error`. The compiled SQL of a ratio
  metric now ends with one extra column, `_canonic_zero_denominator_<metric>`, that `query` strips
  from the result before it is returned. A consumer that executes `compiled.sql` itself sees that
  column and can ignore it. A consumer that ignores unknown fields is unaffected.

## 3.5 (2026-10-18) - MINOR

- ADR/PR: this PR (feat(mcp): return predicate and knowledge references for fired guardrails)
- Summary: Each entry of `guardrails_fired` (`QueryResult.metadata`, compile output) gains two
  optional fields. `predicate` is the filter or constraint the guardrail applied: the filter of a
  `mandatory_filter`, `group by or filter on <dimension>`, `source role = <role>` or
  `trust tier >= <level>`. `knowledge_refs` lists the ids of the global knowledge pages that
  explain the guardrail, sorted, and `[]` when there are none. A page matches when its `sl_refs`
  overlap the entities the guardrail reads (its measure, the columns of its filter, its
  dimension), or when the guardrail names it in the new optional contract field
  `knowledge_refs`, which replaces the inferred match. Pages the caller's role may not read are
  never listed. A consumer that ignores unknown fields is unaffected.

## 3.4 (2026-10-18) - MINOR

- ADR/PR: this PR (feat(compiler): expose composition metadata per metric in the query result)
- Summary: `QueryResult.metadata` and the compile output gain an optional `composition` map, one
  entry per composite metric (`ratio`, `weighted_avg`) of the query, keyed by the requested metric
  name. Each entry carries `kind`, `on_zero_denominator` and the `numerator` and `denominator`,
  each with its `metric`, its `resolved` binding and the `population_filter` its leaf was compiled
  over (the composite's own filter and the component's, ANDed when both are declared). The map is
  empty for a query without a composite metric. A consumer that ignores unknown fields is
  unaffected.

## 3.3 (2026-10-18) - MINOR

- ADR/PR: this PR (fix(mcp): return binding details for composite metrics)
- Summary: `resolve_metric` and `describe_metric` now say how a metric is built. Both add `kind`,
  `components` (`numerator`, `denominator`, `on_zero_denominator` for a `ratio` or `weighted_avg`,
  `null` for any other kind) and `population_filter`. `source` and `measure` stay `null` for a
  composite, so existing callers keep working. For a composite, `describe_metric` lists the
  measures its components read in `measures` (it listed every measure of the owning sources) and
  returns the shared `grain` when both components sit on one source (it was always empty). All
  additions are optional, and a consumer that ignores unknown fields is unaffected.

## 3.2 (2026-10-14) - MINOR

- ADR/PR: this PR (feat(tenancy): scope a principal to a list of tenants)
- Summary: A principal can now carry several tenants, compiled to `<column> IN (...)`.
  `metadata.scope.tenant` (`QueryResult`, compile output) and `AnswerEvent.tenant` widen from
  `string | null` to `string | integer | array of (string | integer) | null`. A principal with
  exactly one tenant is reported as the bare value as before, so a consumer sees a list only
  for a caller that could not be scoped to several tenants before. The tenant keeps the type
  the identity provider sent (an integer claim is an integer). Adds the `tenant_limit_exceeded`
  error (exit 25), raised when a token carries more tenants than `mcp.auth.max_tenants`. A
  consumer that types `tenant` as a string must accept a list to handle multi-tenant callers.

## 3.1 (2026-10-11) - MINOR

- ADR/PR: this PR (feat(compiler): add history option to cumulative metrics)
- Summary: the `cumulative` block in `QueryMetadata` gains `history` (`exclude` or `include`),
  the binding's setting for rows that a filter on the order columns excludes. Under the new
  default `exclude`, every query filter applies before accumulating, so a filtered running
  total starts at zero and `visibility_filters` is empty. `include` keeps the behaviour of 3.0,
  where such a filter only picks which rows are shown. A filtered cumulative query on a
  binding without `history` therefore returns different numbers than under 3.0, and
  `history: include` restores them. The new field lives in the contract file format. No
  request shape changes and no new error codes. Classified MINOR under §4.1: an additive
  response field.

## 3.0 (2026-10-10) - MAJOR

- ADR/PR: this PR (feat(compiler)!: name result columns after the metric)
- Summary: every metric column in a result is named after the metric the query asked for.
  Before, `single` metrics and `semi_additive` metrics grouped by their collapse dimension
  came out under their measure's name, so two metrics bound to one measure produced two
  columns with the same name and a client could not tell them apart. `ratio`, `weighted_avg`,
  `distinct_count`, `percentile`, `opaque` and `cumulative` metrics already used the metric
  name. The request shape, `QueryMetadata` and the error registry are unchanged, and so are
  the values. Assertions now match `expect.values` keys against result columns directly.
  Classified MAJOR under §4.2: a client that reads a result column by its measure name
  breaks.

## 2.13 (2026-10-10) - MINOR

- ADR/PR: this PR (feat(compiler): add cumulative metrics)
- Summary: `QueryMetadata` gains an optional `cumulative` block, `null` unless the query
  requests a `cumulative` metric, and otherwise `{"metric", "order_by", "partition_by",
  "on_gap", "visibility_filters"}` for the first one. `partition_by` is resolved per query,
  which is what an agent needs to explain a running total. The new binding kind and its
  `order_by`/`on_gap` fields live in the contract file format and do not bump on their own.
  No request shape changes and no new error codes. Classified MINOR under §4.1: an additive
  response field.

## 2.12 (2026-10-06) - MINOR

- ADR/PR: this PR (feat(knowledge): wire ingest-time reference pruning and the staleness signal)
- Summary: `read_knowledge_page` returns `meta.staleness`, `null` while the page's references
  were validated within `knowledge.staleness_window_days` (default 90) and otherwise
  `{"age_days", "message"}`. Additive field on an existing response, no existing field changes.
  Classified MINOR under §4.1.

## 2.11 (2026-10-01) - MINOR

- ADR/PR: this PR (feat(compiler): allow distinct_count components in ratio metrics,
  AMENDMENT-ratio-recompute-components, S1-S5)
- Summary: No change to any frozen field set. A `ratio` or `weighted_avg` whose numerator
  or denominator is a `distinct_count` metric now compiles, where it previously failed with
  `unsupported_measure`. Each component is planned by the strategy for its own kind and
  aggregates to the requested grain before the division. `percentile`, `semi_additive` and
  composite components are still rejected, now with a message naming the component and its
  kind. Classified MINOR under §4.1: a request that used to fail now succeeds, and no
  request or response shape changes.

## 2.10 (2026-09-23) - MINOR

- ADR/PR: this PR (feat(mcp): derive the Principal from identity-asserted tokens,
  AMENDMENT-e12-identity-assertion-principal, S25-S28)
- Summary: `AnswerEvent.user` may now also be an object `{"subject", "acted_via"}`.
  An identity-asserted call (SEP-990 ID-JAG) records the asserted employee, whose
  `Principal` scoped the query, as `subject`, and the calling agent's own `client_id`
  as `acted_via`. Static-token, `JWTVerifier` and `OIDCProxy` calls keep the existing
  plain-string shape, and stdio/CLI calls keep `null`. Classified MINOR under §4.1:
  the new shape only appears on a new auth path, and no existing emitter changes shape.

## 2.9 (2026-09-22) - MINOR

- ADR/PR: this PR (feat(mcp): advertise contract_schema as an MCP server extension,
  AMENDMENT-fastmcp4-adoption §2, S19)
- Summary: No change to any frozen field set. Recorded because `contract_schema` is
  now also advertised under `capabilities.extensions["app.getcanonic/contract"]` on
  both MCP protocol eras, and enforced per request for clients that opt in, in
  addition to the existing `contract_info`/`negotiate_contract` tools kept as a
  fallback.

## 2.8 (2026-08-17) - MINOR

- ADR/PR: this PR (feat(core): tenant scoping and role-based authorization error
  registry + AnswerEvent attribution, SPEC-E12 §3, §5, §6)
- Summary: Adds `tenant_forbidden` (exit 24) to the error registry — raised by the
  `run_sql` gate when a role denies raw SQL execution (`run_sql: false`) or when
  tenancy is enabled and the target connection carries no `rls_enforced: true`
  attestation. `TENANT_UNRESOLVED` (22) and `TENANT_SCOPE_MISSING` (23) were already
  reserved on `ErrorCode`/`EXIT_CODES` ahead of this bump (compiler-stage raises, SPEC-E12
  §3). `AnswerEvent` gains three nullable fields — `tenant`, `roles`, `tenancy_exempt` —
  populated from the verified `Principal` on every served answer and raw-SQL execution,
  following the same reserved-field discipline as `cache_hit`/`over_limit_blocked`. No
  change to `SemanticQuery`: a `Principal` is bound by the adapter from a verified token,
  never accepted as a query field (SPEC-E12, "authorization is a compiler input, never a
  query field"). Classified MINOR under §4.1: two new error codes and three new nullable
  event fields are additive; no existing field changes shape and every prior code keeps
  its exit value.

## 2.7 (2026-08-14) - MINOR

- ADR/PR: this PR (feat(reports): curated reports — list_reports/run_report,
  AMENDMENT-curated-reports)
- Summary: Adds a fourth committed directory, `reports/`, and two capabilities —
  `list_reports(domain?)` and `run_report(report_id, as_of?)` — plus their CLI
  (`canonic report list`/`canonic report run`) and MCP (`list_reports`/`run_report`)
  adapters. A report is a named, ordered sequence of sections, each an unmodified
  `SemanticQuery` run through the existing `query()` capability, with an optional
  narrative attached via the existing `read_page` capability. `run_report` introduces
  zero new execution semantics (a deterministic loop over `core.query`) and zero new
  authority (a report cannot declare a canonical binding, introduce a metric, or relax
  a guardrail — PRD §5.1's three-way split is unchanged). A failing section resolves to
  the existing `{code, message, candidates?}` error shape in place of a result rather
  than aborting the run; an unknown `report_id` reuses the existing `unresolved` wire
  code. No new error code, no change to `SemanticQuery`, `QueryResult`, `CompileOutput`,
  or the error registry — classified MINOR under §4.1 because two new capabilities and
  two new MCP tools are additive surface a client may want to negotiate on before
  relying on them.

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
