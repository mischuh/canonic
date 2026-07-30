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

## Golden suite

`tests/golden/` locks the compiled SQL **and** the executed numbers for a set of
queries against the shipped example projects (jaffle-shop, dutch-railway,
saas-analytics, rental -- no Docker, no network). Each case reproduces the shape of
a past `fix(compiler)` bug, so a regression shows up as a golden diff instead of
someone noticing a wrong number by hand.

If a golden fails, first decide whether the *number* is now right or wrong. Only
after that, regenerate and review the diff:

```
uv run pytest tests/golden -x                 # see what moved
uv run pytest tests/golden --regen-golden      # rewrite the locked artifacts
git diff tests/golden/golden/                   # THE review artifact
```

Adding a case: append one line to the relevant `tests/golden/cases/*.jsonl` file
with a `why` naming the commit/bug it pins, then run `--regen-golden` to generate
its `.sql`/`.json` artifacts.

## `contract_schema` changes

Changes to `CONTRACT_SCHEMA` (`canonic/contract.py`) are a special case and
are **never** driven by Conventional Commits or release-please. Follow the
process in `docs/SPEC-P0-interface-freeze.md` §7: open an ADR classifying
the change as MINOR or MAJOR, and add an entry to `CONTRACT_CHANGELOG.md` in
the same PR. CI (`.github/workflows/contract-schema-guard.yml`) fails any PR
that changes `CONTRACT_SCHEMA` without a matching `CONTRACT_CHANGELOG.md`
entry.
