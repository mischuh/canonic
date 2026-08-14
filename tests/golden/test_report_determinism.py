"""Report determinism against a real committed example project (S16 AC4).

Reuses the session-scoped ``golden_services`` fixture (see conftest.py): the same
jaffle-shop project the semantic-correctness golden suite runs against, with its
committed, read-only-opened DuckDB file. No new fixtures, no live network DB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from canonic.core.service import CanonicService

pytestmark = pytest.mark.release_gate


async def test_customer_report_runs_and_validates(
    golden_services: dict[str, CanonicService],
) -> None:
    service = golden_services["jaffle-shop"]

    service.validate_reports()  # must not raise

    result = await service.run_report("customer_report")
    assert result.report_id == "customer_report"
    assert len(result.sections) == 5
    for section in result.sections:
        assert section.error is None
        assert section.result is not None

    # First three sections carry a narrative_from; the last two don't (report-schema mix).
    with_narrative = result.sections[:3]
    without_narrative = result.sections[3:]
    for section in with_narrative:
        assert section.narrative is not None
        assert "{{" not in section.narrative.body
    for section in without_narrative:
        assert section.narrative is None


async def test_repeated_runs_are_byte_identical(
    golden_services: dict[str, CanonicService],
) -> None:
    """S16 AC4: same as_of, same committed state -> byte-identical section results.

    "Determinism" here is the compiler's guarantee (SPEC-E5 step 9): byte-identical SQL
    for a repeated query, asserted directly on ``compiled.sql``. Row order for an
    unordered ``GROUP BY`` is a database execution detail outside that guarantee — the
    golden suite's own ``canonicalize_rows`` (tests/golden/runner.py) makes the same
    call — so rows are compared as a canonicalized (sorted) set, matching that
    established convention rather than asserting a row order the compiler never promised.
    """
    import json
    from datetime import UTC, datetime

    from tests.golden.runner import canonicalize_rows

    service = golden_services["jaffle-shop"]
    as_of = datetime(2026, 1, 1, tzinfo=UTC)

    first = await service.run_report("customer_report", as_of=as_of)
    second = await service.run_report("customer_report", as_of=as_of)

    a = first.model_dump(mode="json")
    b = second.model_dump(mode="json")
    for section_a, section_b in zip(a["sections"], b["sections"], strict=True):
        assert section_a["result"]["compiled"]["sql"] == section_b["result"]["compiled"]["sql"]
        rows_a = canonicalize_rows(section_a["result"]["result"]["rows"], ordered=False)
        rows_b = canonicalize_rows(section_b["result"]["result"]["rows"], ordered=False)
        assert rows_a == rows_b
        section_a["result"]["result"]["rows"] = rows_a
        section_b["result"]["result"]["rows"] = rows_b

    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
