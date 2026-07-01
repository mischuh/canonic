"""Tests for ``canon review`` (GH-150, AC1/AC2/AC4/AC5)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from canon.cli.app import app
from canon.ingestion.emitter import DiffEmitter
from canon.ingestion.models import (
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canon.ingestion.pending import PendingDiffStore, PendingRun, ProposalStatus
from canon.semantic.models import Provenance

if TYPE_CHECKING:
    from pathlib import Path


_CONFIG = """\
version: 1
project:
  name: test-project
  default_connection: warehouse_pg
connections:
  - id: warehouse_pg
    type: postgres
    params: {host: localhost, port: 5432, user: u, dbname: db}
    credentials_ref: env:CANON_PW
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
"""

_TARGET_YAML = "semantics/warehouse_pg/orders.yaml"

_VALID_SOURCE_CONTENT = {
    "name": "orders",
    "connection": "warehouse_pg",
    "table": "public.orders",
    "grain": ["order_id"],
    "columns": [{"name": "order_id", "type": "int", "nullable": False}],
}


def _proposal(
    *,
    target: str = _TARGET_YAML,
    op: ProposalOp = ProposalOp.ADD,
    content: dict[str, Any] | None = None,
) -> Proposal:
    return Proposal(
        target=target,
        op=op,
        content=content or _VALID_SOURCE_CONTENT,
        provenance=Provenance.INFERRED,
        confidence=0.95,
        anchored_to=["sha256:abc"],
    )


def _entry(
    decision: ReconciliationDecision = ReconciliationDecision.ADD,
    *,
    target: str = _TARGET_YAML,
    content: dict[str, Any] | None = None,
    existing: dict[str, Any] | None = None,
) -> ReconciliationEntry:
    return ReconciliationEntry(
        decision=decision,
        target=target,
        proposal=_proposal(target=target, content=content),
        existing=existing,
    )


def _write_run(project_root: Path, n: int = 1) -> Path:
    """Write a pending-diff run with ``n`` ADD proposals and return the run dir."""
    entries = [_entry(target=f"semantics/warehouse_pg/table_{i}.yaml") for i in range(1, n + 1)]
    report = ReconciliationReport(entries=entries)
    emission = DiffEmitter().emit(report)
    return PendingDiffStore(project_root).write("20260626T143201Z", emission)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A valid canon project in tmp_path; cwd is set to it."""
    from canon.config import scaffold_project

    (tmp_path / "canon.yaml").write_text(_CONFIG)
    scaffold_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestReviewNoRuns:
    def test_exits_with_message_when_no_runs(self, project: Path) -> None:
        result = CliRunner().invoke(app, ["review"])
        assert result.exit_code == 1
        assert "no pending-diff runs found" in result.output

    def test_exits_with_message_for_unknown_run_id(self, project: Path) -> None:
        result = CliRunner().invoke(app, ["review", "--run-id", "nope"])
        assert result.exit_code == 1
        assert "run-id not found" in result.output


class TestReviewMenuRendering:
    def test_actions_menu_renders_literal_brackets(self, project: Path) -> None:
        """Regression: the action hints must render as literal [a]/[r]/[s]/[f]/[q],
        not be swallowed by Rich markup parsing (double-bracket escaping is broken)."""
        _write_run(project)
        result = CliRunner().invoke(app, ["review"], input="q\n")

        assert result.exit_code == 0, result.output
        assert "[a]ccept / [r]eject / [s]kip / [f]reeze / [q]uit" in result.output

    def test_proposal_line_renders_literal_brackets(self, project: Path) -> None:
        _write_run(project)
        result = CliRunner().invoke(app, ["review"], input="q\n")

        assert result.exit_code == 0, result.output
        # Normalize away Rich's console-width word-wrapping before matching.
        normalized = " ".join(result.output.split())
        assert "[add, confidence: 0.95, deterministic]" in normalized


class TestReviewAccept:
    def test_ac1_accept_writes_target_file(self, project: Path) -> None:
        """AC1: accepting a proposal writes the target file to the working directory."""
        _write_run(project)
        result = CliRunner().invoke(app, ["review"], input="a\n")

        assert result.exit_code == 0, result.output
        assert (project / _TARGET_YAML.replace("table_1.yaml", "table_1.yaml")).exists() or (
            project / "semantics" / "warehouse_pg" / "table_1.yaml"
        ).exists()

    def test_ac2_no_git_command_is_invoked(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2: no git add/commit invoked — only file writes happen."""
        git_calls: list[str] = []

        import subprocess

        original_run = subprocess.run

        def spy_run(args: Any, **kwargs: Any) -> Any:
            if isinstance(args, (list, tuple)) and args and args[0] == "git":
                git_calls.append(str(args))
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy_run)

        _write_run(project)
        CliRunner().invoke(app, ["review"], input="a\n")

        assert git_calls == [], f"unexpected git calls: {git_calls}"

    def test_accept_updates_status_to_accepted(self, project: Path) -> None:
        run_dir = _write_run(project)
        CliRunner().invoke(app, ["review"], input="a\n")

        run = PendingRun.load(run_dir)
        assert run.proposals[0].status is ProposalStatus.ACCEPTED


class TestReviewReject:
    def test_reject_does_not_write_file(self, project: Path) -> None:
        _write_run(project)
        result = CliRunner().invoke(app, ["review"], input="r\n")

        assert result.exit_code == 0, result.output
        assert not (project / "semantics" / "warehouse_pg" / "table_1.yaml").exists()

    def test_reject_updates_status(self, project: Path) -> None:
        run_dir = _write_run(project)
        CliRunner().invoke(app, ["review"], input="r\n")

        run = PendingRun.load(run_dir)
        assert run.proposals[0].status is ProposalStatus.REJECTED


class TestReviewQuit:
    def test_ac4_quit_leaves_remaining_pending(self, project: Path) -> None:
        """AC4: quit leaves remaining proposals as pending; re-run resumes at first pending."""
        run_dir = _write_run(project, n=3)

        CliRunner().invoke(app, ["review"], input="a\nq\n")

        run = PendingRun.load(run_dir)
        assert run.proposals[0].status is ProposalStatus.ACCEPTED
        assert run.proposals[1].status is ProposalStatus.PENDING
        assert run.proposals[2].status is ProposalStatus.PENDING

    def test_ac4_resume_skips_already_decided(self, project: Path) -> None:
        """AC4: re-running review does not re-prompt already-decided items."""
        run_dir = _write_run(project, n=3)

        CliRunner().invoke(app, ["review"], input="a\nq\n")
        CliRunner().invoke(app, ["review"], input="r\nq\n")

        run = PendingRun.load(run_dir)
        assert run.proposals[0].status is ProposalStatus.ACCEPTED
        assert run.proposals[1].status is ProposalStatus.REJECTED
        assert run.proposals[2].status is ProposalStatus.PENDING


class TestReviewFreeze:
    def test_ac5_freeze_writes_frozen_annotation(self, project: Path) -> None:
        """AC5: freeze writes the file and sets meta.frozen=true."""
        from canon.semantic.loader import load_semantic_source

        _write_run(project)
        result = CliRunner().invoke(app, ["review"], input="f\n")

        assert result.exit_code == 0, result.output
        target = project / "semantics" / "warehouse_pg" / "table_1.yaml"
        assert target.exists()
        source = load_semantic_source(target)
        assert source.meta.frozen is True

    def test_freeze_updates_status_to_frozen(self, project: Path) -> None:
        run_dir = _write_run(project)
        CliRunner().invoke(app, ["review"], input="f\n")

        run = PendingRun.load(run_dir)
        assert run.proposals[0].status is ProposalStatus.FROZEN


class TestReviewNothingPending:
    def test_exits_cleanly_when_all_resolved(self, project: Path) -> None:
        _write_run(project)
        CliRunner().invoke(app, ["review"], input="a\n")
        result = CliRunner().invoke(app, ["review"])

        assert result.exit_code == 0
        assert "nothing to review" in result.output
