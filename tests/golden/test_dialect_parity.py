"""Cross-dialect parity: the same semantic query must give the same numbers on every engine.

The golden suite pins each example project on the one engine it ships with. Authors write
``expr`` in their warehouse's SQL and canonic does not transpile it, so nothing else would
notice a metric that means one thing on SQLite and another on DuckDB. Here the rental
project runs on DuckDB and on Snowflake too and every result is compared to its committed
SQLite golden.

Snowflake runs on ``fakesnow``, an in-process emulator that translates to DuckDB. It proves
the compiled Snowflake SQL parses, executes and yields the same numbers, not how the real
warehouse behaves. Live behavior stays with the connector's own live tests.

A difference is either a bug to fix or a documented, deliberate divergence listed in
``_KNOWN_DIVERGENCES`` with the reason. It is never silently tolerated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import fakesnow
import pytest
import snowflake.connector

from canonic.core.service import CanonicService

from .cases import GoldenCase, load_all_cases
from .conftest import EXAMPLES_ROOT
from .parity import (
    SNOWFLAKE_DATABASE,
    SNOWFLAKE_PASSWORD_ENV,
    SNOWFLAKE_SCHEMA,
    comparable_rows,
    seed_statements,
    write_duckdb_rental_project,
    write_snowflake_rental_project,
)
from .runner import run_case

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.release_gate

_GOLDEN_DIR = Path(__file__).parent / "golden"
_SETUP_SQL = EXAMPLES_ROOT / "rental" / "setup.sql"

#: The engines compared against the SQLite golden, each with a ``rental_on_<engine>`` fixture.
_ENGINES = ("duckdb", "snowflake")

_MEDIAN_REASON = (
    "SQLite has no PERCENTILE_CONT, so the compiler falls back to nearest-rank and returns "
    "an actual row value (489.93). The other engines interpolate between the two middle "
    "values of the 32 rows (544.945), and DuckDB, which also backs the Snowflake emulator, "
    "hands that back at the column's DECIMAL scale (544.94). The SQLite result already "
    "carries a warning about the interpolation."
)

#: (engine, case id) -> why that engine is allowed to differ from the SQLite golden.
_KNOWN_DIVERGENCES: dict[tuple[str, str], str] = {
    ("duckdb", "rental_median_rental_amount"): _MEDIAN_REASON,
    ("snowflake", "rental_median_rental_amount"): _MEDIAN_REASON,
}

_PARITY_CASES = [
    c
    for c in load_all_cases()
    if c.project == "rental" and c.expect_error is None and not c.compile_only
]


@pytest.fixture(scope="session")
def rental_on_duckdb(tmp_path_factory: pytest.TempPathFactory) -> CanonicService:
    dest = tmp_path_factory.mktemp("rental_duckdb")
    write_duckdb_rental_project(EXAMPLES_ROOT / "rental", dest, _SETUP_SQL.read_text())
    return CanonicService.from_project(dest)


@pytest.fixture
def rental_on_snowflake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[CanonicService]:
    """The rental project on the emulator, which only exists while ``fakesnow`` is patched in."""
    monkeypatch.setenv(SNOWFLAKE_PASSWORD_ENV, "unused")
    write_snowflake_rental_project(EXAMPLES_ROOT / "rental", tmp_path)
    with fakesnow.patch():
        seed = snowflake.connector.connect(database=SNOWFLAKE_DATABASE, schema=SNOWFLAKE_SCHEMA)
        cursor = seed.cursor()
        for statement in seed_statements(_SETUP_SQL.read_text(), "snowflake", identify=True):
            cursor.execute(statement)
        seed.close()
        yield CanonicService.from_project(tmp_path)


@pytest.mark.parametrize("case", _PARITY_CASES, ids=lambda c: c.id)
@pytest.mark.parametrize("engine", _ENGINES)
async def test_engine_matches_sqlite_golden(
    engine: str, case: GoldenCase, request: pytest.FixtureRequest
) -> None:
    service: CanonicService = request.getfixturevalue(f"rental_on_{engine}")
    expected = json.loads((_GOLDEN_DIR / case.project / f"{case.id}.json").read_text())

    outcome = await run_case(case, service)

    assert outcome.result_doc is not None
    assert outcome.result_doc["dialect"] == engine
    assert [c["name"] for c in outcome.result_doc["columns"]] == [
        c["name"] for c in expected["columns"]
    ]
    actual_rows = comparable_rows(outcome.result_doc["rows"])
    expected_rows = comparable_rows(expected["rows"])
    if (engine, case.id) in _KNOWN_DIVERGENCES:
        assert actual_rows != expected_rows, (
            f"{case.id!r} no longer diverges on {engine}, remove it from _KNOWN_DIVERGENCES"
        )
        return
    assert actual_rows == expected_rows, (
        f"{case.id!r} gives different numbers on {engine} than on SQLite. First check that "
        f"the example's semantics mean the same on both, then fix the compiler or list the "
        f"case in _KNOWN_DIVERGENCES with the reason."
    )


def test_known_divergences_name_real_cases() -> None:
    known = {(e, c.id) for e in _ENGINES for c in _PARITY_CASES}
    stale = set(_KNOWN_DIVERGENCES) - known
    assert not stale, f"_KNOWN_DIVERGENCES lists unknown engine/case pairs: {sorted(stale)}"
