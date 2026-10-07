"""Ingest-time knowledge-page reference upkeep (SPEC-E6 §3.2 S5, §8 S8).

A page whose reference disappeared gets a propose-only ``EDIT`` (never a silent edit), a page
whose references all resolve gets its ``last_validated_at`` refreshed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from canonic.ingestion.models import ProposalOp, ReconciliationDecision
from canonic.ingestion.source import evidence_from_introspection
from canonic.knowledge.loader import load_knowledge_page
from tests.ingestion.test_pipeline import (
    _CONN,
    FakeConnector,
    _customers,
    _orders,
    _pipeline,
    _pipeline_no_scaffold,
)

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.ingestion.pipeline import IngestionPipeline, PipelineResult

_LIVE = f"{_CONN}.fct_orders"
_GONE = f"{_CONN}.fct_orders.gone_measure"


def _write_page(
    root: Path,
    name: str,
    *,
    sl_refs: list[str],
    refs: list[str] | None = None,
    extra_meta: str = "",
) -> Path:
    path = root / "knowledge" / "global" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    sl = "".join(f"  - {r}\n" for r in sl_refs)
    rf = "".join(f"  - {r}\n" for r in refs or [])
    sl_block = f"sl_refs:\n{sl}" if sl_refs else ""
    refs_block = f"refs:\n{rf}" if refs else ""
    meta_block = f"meta:\n{extra_meta}" if extra_meta else ""
    path.write_text(
        f'---\nsummary: "{name}"\n{sl_block}{refs_block}{meta_block}---\n\nBody of {name}.\n'
    )
    return path


async def _rerun(pipeline: IngestionPipeline, *, dry_run: bool = False) -> PipelineResult:
    evidence = await evidence_from_introspection(FakeConnector([_customers(), _orders()]), _CONN)
    return await pipeline.run(evidence, dry_run=dry_run)


async def _bootstrapped(root: Path) -> IngestionPipeline:
    pipeline = _pipeline(root, [_customers(), _orders()])
    await pipeline.bootstrap(_CONN)
    return _pipeline_no_scaffold(root, [_customers(), _orders()])


async def test_dangling_sl_ref_is_proposed_for_removal_not_edited(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    page = _write_page(tmp_path, "revenue", sl_refs=[_LIVE, _GONE])
    before = page.read_text()

    result = await _rerun(pipeline)

    (diff,) = result.emission.diffs
    assert diff.target == "knowledge/global/revenue.md"
    assert diff.op is ProposalOp.EDIT
    assert _GONE in (diff.before or "")
    assert _GONE not in (diff.after or "")
    assert _LIVE in (diff.after or "")
    assert page.read_text() == before  # propose-only, the file is untouched


async def test_dangling_page_ref_is_proposed_for_removal(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    _write_page(tmp_path, "other", sl_refs=[_LIVE])
    _write_page(tmp_path, "revenue", sl_refs=[_LIVE], refs=["other", "missing-page"])

    result = await _rerun(pipeline)

    (diff,) = result.emission.diffs
    assert diff.target == "knowledge/global/revenue.md"
    assert "missing-page" not in (diff.after or "")
    assert "other" in (diff.after or "")


async def test_pruned_page_loses_its_validation_stamp(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    stamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    _write_page(
        tmp_path,
        "revenue",
        sl_refs=[_GONE],
        extra_meta=f"  last_validated_at: {stamp}\n",
    )

    result = await _rerun(pipeline)

    (diff,) = result.emission.diffs
    assert stamp not in (diff.after or "")


async def test_frozen_page_is_left_alone(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    _write_page(tmp_path, "revenue", sl_refs=[_GONE], extra_meta="  frozen: true\n")

    result = await _rerun(pipeline)

    assert result.emission.diffs == []


async def test_page_pointing_at_a_page_drafted_in_the_same_run_is_kept(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    _write_page(tmp_path, "revenue", sl_refs=[_LIVE], refs=["existing"])
    _write_page(tmp_path, "existing", sl_refs=[_LIVE])

    result = await _rerun(pipeline)

    assert result.emission.diffs == []
    assert all(e.decision is ReconciliationDecision.NO_OP for e in result.report.entries)


async def test_valid_page_is_stamped_on_a_real_run(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    path = _write_page(tmp_path, "revenue", sl_refs=[_LIVE])
    assert load_knowledge_page(path).meta.last_validated_at is None

    await _rerun(pipeline)

    assert load_knowledge_page(path).meta.last_validated_at is not None


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    path = _write_page(tmp_path, "revenue", sl_refs=[_LIVE])
    before = path.read_text()

    await _rerun(pipeline, dry_run=True)

    assert path.read_text() == before


async def test_recent_stamp_is_not_rewritten(tmp_path: Path) -> None:
    pipeline = await _bootstrapped(tmp_path)
    stamp = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    path = _write_page(
        tmp_path, "revenue", sl_refs=[_LIVE], extra_meta=f"  last_validated_at: {stamp}\n"
    )
    before = path.read_text()

    await _rerun(pipeline)

    assert path.read_text() == before


async def test_page_bound_to_pruned_source_is_proposed_for_pruning(tmp_path: Path) -> None:
    """An entity that disappears from the source system takes the page's reference with it."""
    pipeline = await _bootstrapped(tmp_path)
    _write_page(tmp_path, "customers", sl_refs=[f"{_CONN}.dim_customers"])
    _write_page(tmp_path, "revenue", sl_refs=[_LIVE])

    evidence = await evidence_from_introspection(FakeConnector([_orders()]), _CONN)
    result = await pipeline.run(evidence)

    by_target = {d.target: d for d in result.emission.diffs}
    assert by_target[f"semantics/{_CONN}/dim_customers.yaml"].op is ProposalOp.PRUNE
    page_diff = by_target["knowledge/global/customers.md"]
    assert page_diff.op is ProposalOp.EDIT
    assert f"{_CONN}.dim_customers" not in (page_diff.after or "")
    assert "knowledge/global/revenue.md" not in by_target
