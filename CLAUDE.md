# Project: Canon The Open Context Layer for Data Agents

## Commands
- Run tests: `pytest tests/ -x --tb=short`
- Run single test: `pytest tests/test_foo.py::test_bar -v`
- Lint: `ruff check . && ruff format --check .`
- Type check: `mypy src/`
- All checks: `make check`

## Project structure
- `canon/` — main package code
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
- Custom exceptions live in `canon/exc.py` (package root), not inline in the raising module

## Don't touch
- `migrations/` — hands off unless explicitly asked
- `.env` files — never read or modify
- `pyproject.toml` dependency versions — propose changes, don't apply