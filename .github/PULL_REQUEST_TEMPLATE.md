## Summary

<!-- What changed and why. Link the issue this closes, if any. -->

## Test plan

<!-- How you verified this. `ruff check . && ruff format --check .`, `mypy canonic/`,
and `pytest tests/ -x --tb=short` are required by CI regardless — call out anything
additional you ran (golden suite regen, e2e, manual CLI walkthrough). -->

- [ ] `ruff check . && ruff format --check .`
- [ ] `mypy canonic/`
- [ ] `pytest tests/ -x --tb=short`

---

Checklist:

- [ ] PR title follows [Conventional Commits](../CONTRIBUTING.md#commit-messages) (`feat:`, `fix:`, `docs:`, …) — it drives the changelog and release version on squash-merge.
- [ ] If this changes `CONTRACT_SCHEMA`, `CONTRACT_CHANGELOG.md` is updated in this PR ([CONTRIBUTING.md](../CONTRIBUTING.md#contract_schema-changes)).
- [ ] If this touches compiled SQL or example projects, `tests/golden/` was regenerated and the diff reviewed ([CONTRIBUTING.md](../CONTRIBUTING.md#golden-suite)).
