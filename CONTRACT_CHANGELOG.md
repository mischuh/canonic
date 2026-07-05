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

<!-- Add new entries above this line, most recent first. -->
