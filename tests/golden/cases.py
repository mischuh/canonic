"""Golden case model + JSONL loader for the semantic correctness suite.

Each case is a pure-data request against one of the zero-infrastructure example
projects (jaffle-shop, dutch-railway, saas-analytics, rental) -- a bundled DuckDB
file or a SQLite database built from a tracked ``setup.sql``, so the suite needs no
Docker and no network. Cases are declared in JSONL (one JSON object per line,
``#``-comment lines and blank lines skipped) following the convention already used
by ``canonic/eval/dataset.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

__all__ = ["GoldenCase", "load_all_cases", "load_cases"]

_CASES_DIR = Path(__file__).parent / "cases"


class GoldenCase(BaseModel):
    """One locked query against one example project.

    ``why`` is required: it names the commit or bug this case pins, so
    ``grep <sha> tests/golden/cases/`` answers "is this regression covered?" and
    a coverage meta-test can enforce that no case is ever deleted quietly.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    project: str
    why: str
    query: dict[str, Any]
    compile_only: bool = False
    ordered: bool = False
    expect_error: str | None = None


def load_cases(path: Path) -> list[GoldenCase]:
    """Load golden cases from one JSONL file.

    Raises:
        ValueError: The file is missing, a line is not valid JSON, or a line does
            not satisfy :class:`GoldenCase` -- the message carries the file and
            1-based line number.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read golden case file {path}: {exc}") from exc

    cases: list[GoldenCase] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        try:
            cases.append(GoldenCase.model_validate(payload))
        except ValidationError as exc:
            detail = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            raise ValueError(f"{path}:{lineno}: invalid golden case: {detail}") from exc

    if not cases:
        raise ValueError(f"{path}: no golden cases found")
    return cases


def load_all_cases() -> list[GoldenCase]:
    """Load every golden case from ``tests/golden/cases/*.jsonl``, sorted by id."""
    cases: list[GoldenCase] = []
    for path in sorted(_CASES_DIR.glob("*.jsonl")):
        cases.extend(load_cases(path))
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate golden case ids: {sorted(duplicates)}")
    return sorted(cases, key=lambda c: c.id)
