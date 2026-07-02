"""Tests for ``canonic apply`` (GH-150, AC2/AC3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.ingestion.emitter import DiffEmitter
from canonic.ingestion.models import (
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canonic.ingestion.pending import PendingDiffStore, PendingRun, ProposalStatus
from canonic.semantic.models import Provenance

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
    credentials_ref: env:CANONIC_PW
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
"""


def _proposal(
    *,
    target: str,
    content: dict[str, Any] | None = None,
) -> Proposal:
    return Proposal(
        target=target,
        op=ProposalOp.ADD,
        content=content
        or {
            "name": target.split("/")[-1].replace(".yaml", ""),
            "connection": "warehouse_pg",
            "grain": ["id"],
        },
        provenance=Provenance.INFERRED,
        confidence=0.95,
        anchored_to=["sha256:abc"],
    )


def _write_run(project_root: Path, targets: list[str]) -> Path:
    entries = [
        ReconciliationEntry(
            decision=ReconciliationDecision.ADD,
            target=t,
            proposal=_proposal(target=t),
        )
        for t in targets
    ]
    report = ReconciliationReport(entries=entries)
    emission = DiffEmitter().emit(report)
    return PendingDiffStore(project_root).write("20260626T143201Z", emission)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from canonic.config import scaffold_project

    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    scaffold_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestApplyBasic:
    def test_applies_pending_proposals(self, project: Path) -> None:
        targets = [
            "semantics/warehouse_pg/orders.yaml",
            "semantics/warehouse_pg/customers.yaml",
        ]
        run_dir = _write_run(project, targets)

        result = CliRunner().invoke(app, ["apply", str(run_dir)])

        assert result.exit_code == 0, result.output
        for t in targets:
            assert (project / t).exists(), f"{t} was not written"

    def test_updates_status_to_accepted(self, project: Path) -> None:
        targets = ["semantics/warehouse_pg/orders.yaml"]
        run_dir = _write_run(project, targets)

        CliRunner().invoke(app, ["apply", str(run_dir)])

        run = PendingRun.load(run_dir)
        assert all(p.status is ProposalStatus.ACCEPTED for p in run.proposals)

    def test_reports_applied_count(self, project: Path) -> None:
        targets = [
            "semantics/warehouse_pg/orders.yaml",
            "semantics/warehouse_pg/customers.yaml",
        ]
        run_dir = _write_run(project, targets)

        result = CliRunner().invoke(app, ["apply", str(run_dir)])

        assert "applied 2" in result.output

    def test_ac2_no_git_interaction(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC2: apply never invokes git."""
        git_calls: list[str] = []
        import subprocess

        original_run = subprocess.run

        def spy_run(args: Any, **kwargs: Any) -> Any:
            if isinstance(args, (list, tuple)) and args and args[0] == "git":
                git_calls.append(str(args))
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy_run)

        run_dir = _write_run(project, ["semantics/warehouse_pg/orders.yaml"])
        CliRunner().invoke(app, ["apply", str(run_dir)])

        assert git_calls == []


class TestApplySkips:
    def test_ac3_skips_deleted_diff_file(self, project: Path) -> None:
        """AC3: proposals whose .diff file was deleted are skipped."""
        targets = [
            "semantics/warehouse_pg/orders.yaml",
            "semantics/warehouse_pg/customers.yaml",
        ]
        run_dir = _write_run(project, targets)

        run = PendingRun.load(run_dir)
        from pathlib import Path as _Path

        _Path(run.proposals[0].diff_file).unlink()

        result = CliRunner().invoke(app, ["apply", str(run_dir)])

        assert result.exit_code == 0, result.output
        assert not (project / targets[0]).exists()
        assert (project / targets[1]).exists()
        assert "skipped 1" in result.output

    def test_skips_already_terminal_proposals(self, project: Path) -> None:
        targets = [
            "semantics/warehouse_pg/orders.yaml",
            "semantics/warehouse_pg/customers.yaml",
        ]
        run_dir = _write_run(project, targets)

        CliRunner().invoke(app, ["apply", str(run_dir)])

        (project / targets[1]).unlink()
        result = CliRunner().invoke(app, ["apply", str(run_dir)])

        assert result.exit_code == 0
        assert "applied 0" in result.output
        assert "skipped 2" in result.output

    def test_reports_all_skipped_when_no_pending(self, project: Path) -> None:
        run_dir = _write_run(project, ["semantics/warehouse_pg/orders.yaml"])
        CliRunner().invoke(app, ["apply", str(run_dir)])

        result = CliRunner().invoke(app, ["apply", str(run_dir)])

        assert "applied 0" in result.output
        assert "skipped 1" in result.output


class TestApplyErrors:
    def test_exits_when_no_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(app, ["apply", str(tmp_path / "nonexistent")])

        assert result.exit_code == 1
        assert "no canonic project found" in result.output

    def test_exits_when_run_dir_not_found(self, project: Path) -> None:
        result = CliRunner().invoke(
            app, ["apply", str(project / ".canonic" / "pending-diffs" / "nope")]
        )

        assert result.exit_code == 1
        assert "not found" in result.output
