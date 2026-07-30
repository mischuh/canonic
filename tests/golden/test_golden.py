"""Semantic correctness golden suite.

Locks the compiled SQL *and* the executed numbers for a curated set of queries against
the four zero-infrastructure example projects (jaffle-shop, dutch-railway,
saas-analytics, rental). Every case in ``cases/*.jsonl`` reproduces the shape of a past
`fix(compiler)` bug -- each of which was a silently-wrong-number defect found by hand
-- so the fix stays fixed instead of relying on someone noticing a wrong number again.

Regeneration:
    uv run pytest tests/golden -x                 # see what moved
    uv run pytest tests/golden --regen-golden      # rewrite the locked artifacts
    git diff tests/golden/golden/                  # THE review artifact

If a golden fails, first decide whether the number is now right or wrong. Only then
regenerate and review the diff -- see CONTRIBUTING.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from canonic import exc as canonic_exc

from .cases import GoldenCase, load_all_cases
from .runner import pretty_sql, run_case

if TYPE_CHECKING:
    from canonic.core.service import CanonicService

pytestmark = pytest.mark.release_gate

_GOLDEN_DIR = Path(__file__).parent / "golden"
_MAX_CASES = 80

ALL_CASES = load_all_cases()

_RELATIVE_DATE_RE = re.compile(r"CURRENT_DATE|CURRENT_TIMESTAMP|\bnow\b", re.IGNORECASE)

# Compiler fixes this suite pins as regressions -- see individual cases' `why` for detail.
# 51e424a (composite-leaf dedup) and 97523dd (finality-branch dedup) are not reachable from
# the shipped examples today (no one_to_many join is declared anywhere; see Phase 3 of the
# harness plan) and are deliberately not required here.
_COVERED_SHAS = frozenset(
    {
        "0cfa313",  # aggregate fanned-out metric sources independently before combining
        "392015d",  # plan joins across all requested metrics' sources
        "781ae46",  # scope population_filter/guardrails per metric in multi-metric queries
        "bc2fd92",  # thread each ratio component's own population_filter into its leaf
        "2923fd1",  # partition semi-additive collapse by source grain
        "661f4cd",  # emit SQLite-native date arithmetic instead of INTERVAL/DATE_TRUNC
        "11b1161",  # (this suite) declare snapshot_id dimension on fct_mrr_snapshot
        "ea67aa7",  # (this suite) exclude NULLs from the SQLite percentile fallback population
    }
)


def _sql_path(case: GoldenCase) -> Path:
    return _GOLDEN_DIR / case.project / f"{case.id}.sql"


def _json_path(case: GoldenCase) -> Path:
    return _GOLDEN_DIR / case.project / f"{case.id}.json"


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
async def test_golden(
    case: GoldenCase,
    golden_services: dict[str, CanonicService],
    request: pytest.FixtureRequest,
) -> None:
    service = golden_services[case.project]

    if case.expect_error is not None:
        error_cls = getattr(canonic_exc, case.expect_error)
        from canonic.compiler.query import SemanticQuery

        with pytest.raises(error_cls):
            await service.query(SemanticQuery(**case.query))
        return

    outcome = await run_case(case, service)
    regen = request.config.getoption("--regen-golden")

    sql_path = _sql_path(case)
    if regen:
        sql_path.parent.mkdir(parents=True, exist_ok=True)
        sql_path.write_text(outcome.sql_text)
    else:
        if not sql_path.exists():
            pytest.fail(
                f"no golden SQL for {case.id!r}; run: uv run pytest tests/golden --regen-golden"
            )
        assert outcome.sql_text == sql_path.read_text(), (
            f"compiled SQL changed for {case.id!r} -- if this is intended, run "
            f"'uv run pytest tests/golden --regen-golden' and review the diff"
        )

    if case.compile_only:
        assert outcome.result_doc is None
        return

    assert outcome.result_doc is not None
    if not case.ordered:
        # Structural guarantee: a finality case must pin as_of, or its result silently
        # depends on the wall clock the next time the suite runs.
        if outcome.result_doc["finality"] is not None:
            assert case.query.get("as_of") is not None, (
                f"{case.id!r} produces finality metadata but does not pin as_of "
                f"-- its watermark (and therefore its result) depends on the wall clock"
            )
        assert not _RELATIVE_DATE_RE.search(outcome.sql_text), (
            f"{case.id!r} is result-locked but its SQL contains a relative-date "
            f"expression -- mark it compile_only instead"
        )

    json_path = _json_path(case)
    if regen:
        import json

        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(outcome.result_doc, indent=2, sort_keys=True) + "\n")
    else:
        import json

        if not json_path.exists():
            pytest.fail(
                f"no golden result for {case.id!r}; run: uv run pytest tests/golden --regen-golden"
            )
        expected = json.loads(json_path.read_text())
        assert outcome.result_doc == expected, (
            f"executed result changed for {case.id!r} -- if the NEW number is correct, run "
            f"'uv run pytest tests/golden --regen-golden' and review the diff; if not, this "
            f"is the bug the case was written to catch"
        )


# ---------------------------------------------------------------------------
# Meta-tests: guard the suite's own integrity, not any one query's correctness.
# ---------------------------------------------------------------------------


def test_case_count_bounded() -> None:
    """Keeps default `pytest tests/` fast; exceeding this is a prompt to dedupe cases."""
    assert len(ALL_CASES) <= _MAX_CASES


def test_every_case_declares_why() -> None:
    empty = [c.id for c in ALL_CASES if not c.why.strip()]
    assert not empty, f"cases with empty why: {empty}"


def test_every_case_has_a_matching_golden_file_or_is_error_only() -> None:
    missing = []
    for case in ALL_CASES:
        if case.expect_error is not None:
            continue
        if not _sql_path(case).exists():
            missing.append(str(_sql_path(case)))
        if not case.compile_only and not _json_path(case).exists():
            missing.append(str(_json_path(case)))
    assert not missing, f"cases missing golden artifacts (run --regen-golden): {missing}"


def test_every_golden_file_has_a_matching_case() -> None:
    known_stems = {c.id for c in ALL_CASES}
    orphans = [str(p) for p in _GOLDEN_DIR.rglob("*") if p.is_file() and p.stem not in known_stems]
    assert not orphans, f"orphaned golden artifacts (case was renamed/deleted): {orphans}"


def test_bug_coverage_complete() -> None:
    """Deleting a regression case for one of these fixes must fail CI."""
    all_why = " ".join(c.why for c in ALL_CASES)
    missing = {sha for sha in _COVERED_SHAS if sha not in all_why}
    assert not missing, f"no golden case pins these known bugs: {sorted(missing)}"


def test_golden_conftest_contains_no_skip() -> None:
    """A skip in a fixture here silently disarms the release gate (ci.yml greps for <skipped).

    Scans conftest.py/cases.py/runner.py for an actual ``pytest.skip(`` call -- not this file,
    whose docstrings and this very test legitimately mention the rule by name.
    """
    skip_call = re.compile(r"pytest\.skip\s*\(")
    offenders = []
    for path in Path(__file__).parent.glob("*.py"):
        if path.name == "test_golden.py":
            continue
        if skip_call.search(path.read_text()):
            offenders.append(path.name)
    assert not offenders, f"golden suite fixtures must fail, never skip: {offenders}"


def test_pretty_sql_is_idempotent() -> None:
    """Sanity check on the canonicalization helper itself, not a query."""
    sql = "select 1 as a"
    once = pretty_sql(sql, "postgres")
    twice = pretty_sql(once, "postgres")
    assert once == twice
