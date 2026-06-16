"""Tests for canon/ingestion/autopr.py (GH-38) — headless auto-PR orchestration (SPEC-E4 §6).

Drives :class:`AutoPRPublisher` against a recording fake so the git/gh seam is exercised without
shelling out, and probes :class:`SubprocessPublisher` error translation on a failing command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canon.exc import CanonError
from canon.ingestion.autopr import AutoPRPublisher, SubprocessPublisher
from canon.ingestion.emitter import ContradictionNote, DiffFormat, EmissionResult, EmittedDiff
from canon.ingestion.models import ProposalOp, ReconciliationReport
from canon.ingestion.pipeline import PipelineResult
from canon.semantic.models import Provenance

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


class _FakePublisher:
    """Records git/gh operations instead of running them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_branch(self, name: str) -> None:
        self.calls.append(("branch", name))

    async def stage(self, paths: Iterable[str]) -> None:
        self.calls.append(("stage", list(paths)))

    async def commit(self, message: str) -> None:
        self.calls.append(("commit", message))

    async def open_pr(self, title: str, body: str) -> str:
        self.calls.append(("open_pr", body))
        return "https://example.test/pr/7"

    async def comment(self, pr_ref: str, body: str) -> None:
        self.calls.append(("comment", body))


def _diff(target: str, op: ProposalOp, after: str | None) -> EmittedDiff:
    return EmittedDiff(
        target=target,
        op=op,
        format=DiffFormat.YAML,
        before=None if op is ProposalOp.ADD else "old\n",
        after=after,
        patch="--- a\n+++ b\n",
        provenance=Provenance.INFERRED,
        confidence=1.0,
    )


def _result(
    diffs: list[EmittedDiff], notes: list[ContradictionNote] | None = None
) -> PipelineResult:
    emission = EmissionResult(
        diffs=diffs, notes=notes or [], report=ReconciliationReport(entries=[])
    )
    return PipelineResult(emission=emission)


async def test_publish_runs_full_sequence_and_writes_files(tmp_path: Path) -> None:
    """A run with diffs branches, materializes the add, stages, commits, and opens the PR."""
    fake = _FakePublisher()
    result = _result([_diff("semantics/c/a.yaml", ProposalOp.ADD, "name: a\n")])

    pr_ref = await AutoPRPublisher(tmp_path, fake).publish(result)

    assert pr_ref == "https://example.test/pr/7"
    assert [c[0] for c in fake.calls] == ["branch", "stage", "commit", "open_pr"]
    assert (tmp_path / "semantics" / "c" / "a.yaml").read_text() == "name: a\n"


async def test_publish_unlinks_pruned_targets(tmp_path: Path) -> None:
    """A prune diff removes its target file when the auto-PR materializes diffs."""
    stale = tmp_path / "semantics" / "c" / "old.yaml"
    stale.parent.mkdir(parents=True)
    stale.write_text("name: old\n")
    result = _result([_diff("semantics/c/old.yaml", ProposalOp.PRUNE, None)])

    await AutoPRPublisher(tmp_path, _FakePublisher()).publish(result)

    assert not stale.exists()


async def test_publish_posts_contradiction_comment(tmp_path: Path) -> None:
    """Flagged contradictions are posted as a single review comment on the PR (§5.4)."""
    fake = _FakePublisher()
    note = ContradictionNote(
        target="semantics/c/a.yaml",
        incoming={"x": 1},
        incoming_provenance=Provenance.INFERRED,
        existing_provenance=Provenance.HUMAN_CURATED,
    )
    result = _result([_diff("semantics/c/b.yaml", ProposalOp.ADD, "name: b\n")], notes=[note])

    await AutoPRPublisher(tmp_path, fake).publish(result)

    comment = next(body for kind, body in fake.calls if kind == "comment")
    assert "Contradictions" in str(comment)


async def test_publish_noop_without_diffs(tmp_path: Path) -> None:
    """No emitted diffs → no PR opened (idempotency, §7)."""
    fake = _FakePublisher()

    pr_ref = await AutoPRPublisher(tmp_path, fake).publish(_result([]))

    assert pr_ref is None
    assert fake.calls == []


async def test_subprocess_publisher_raises_on_failure(tmp_path: Path) -> None:
    """A non-zero subprocess exit surfaces as a structured CanonError, never a traceback."""
    publisher = SubprocessPublisher(tmp_path)

    with pytest.raises(CanonError):
        await publisher._run("sh", "-c", "exit 1")
