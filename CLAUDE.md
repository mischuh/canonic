# Project: Canonic The Open Context Layer for Data Agents

## Commands
- Run tests: `pytest tests/ -x --tb=short`
- Run single test: `pytest tests/test_foo.py::test_bar -v`
- Lint: `ruff check . && ruff format --check .`
- Type check: `mypy src/`
- All checks: `make check`

## Before every commit
Run all three checks and fix any issues before committing — CI enforces all of them:
```
ruff check . && ruff format --check .
mypy canonic/
pytest tests/ -x --tb=short
```

## Project structure
- `canonic/` — main package code
- `tests/` — pytest tests (mirrors src/ structure)
- `scripts/` — one-off scripts, not production code

## Conventions
- Use typed, object oriented python code
- Make use of SOLID principle, whenever it make sense
- Make use of async, whenever it makes sense
- Type hints on all public functions
- Docstrings on public classes and non-obvious functions
- Tests use fixtures from conftest.py — check there before creating new ones
- No bare `except:` — always catch specific exceptions
- Custom exceptions live in `canonic/exc.py` (package root), not inline in the raising module
- Enum members (including `StrEnum`) use UPPER_CASE names; string values stay lowercase if needed for serialization

## Don't touch
- `migrations/` — hands off unless explicitly asked
- `.env` files — never read or modify
- `pyproject.toml` dependency versions — propose changes, don't apply


## Considerations
- If I come up with errors from examples please check first if the semantics are correct (especially joins) before changing code base