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
