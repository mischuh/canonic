# Contributing

## Commit messages

This repo uses [Conventional Commits](https://www.conventionalcommits.org/)
for all commit messages, enforced on pull requests by `commitlint`
(`.github/workflows/commitlint.yml`). Release automation
(`.github/workflows/release-please.yml`) parses commit history to decide the
next version and to populate `CHANGELOG.md`, so commit messages are the
source of truth for what changed and how significant it was.

Format: `<type>[optional scope]: <description>`

- `feat:` — a new feature. Triggers a MINOR version bump.
- `fix:` — a bug fix. Triggers a PATCH version bump.
- `feat!:` or a `BREAKING CHANGE:` footer — an incompatible change. Triggers
  a MAJOR version bump.

Other types (`docs:`, `chore:`, `refactor:`, `test:`, `ci:`) don't trigger a
release but are still linted for format.

### Examples

```
feat: add duckdb adapter for query planning
```

```
fix(mcp): correct contract_schema negotiation for v1 clients
```

```
feat!: remove deprecated compile_output.legacy_shape field

BREAKING CHANGE: compile_output.legacy_shape has been removed. Consumers
must migrate to compile_output.shape, which has been stable since v1.4.
```

## `contract_schema` changes

Changes to `CONTRACT_SCHEMA` (`canonic/contract.py`) are a special case and
are **never** driven by Conventional Commits or release-please. Follow the
process in `docs/SPEC-P0-interface-freeze.md` §7: open an ADR classifying
the change as MINOR or MAJOR, and add an entry to `CONTRACT_CHANGELOG.md` in
the same PR. CI (`.github/workflows/contract-schema-guard.yml`) fails any PR
that changes `CONTRACT_SCHEMA` without a matching `CONTRACT_CHANGELOG.md`
entry.
