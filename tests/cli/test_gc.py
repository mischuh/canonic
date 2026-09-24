"""Tests for ``canonic gc``: dry-run by default, config-driven event retention, opt-in run pruning."""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

from canonic.cli.app import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner


def _segment(project: Path, stamp: str) -> Path:
    state = project / ".canonic"
    state.mkdir(exist_ok=True)
    path = state / f"events-{stamp}.jsonl"
    path.write_text('{"kind": "served_answer"}\n')
    return path


def _run(project: Path, name: str, statuses: list[str], age_days: int) -> Path:
    run_dir = project / ".canonic" / "pending-diffs" / name
    run_dir.mkdir(parents=True)
    lines = ["run_id: " + name, "proposals:"]
    for i, status in enumerate(statuses, start=1):
        lines += [
            f"  - id: '{i:04d}'",
            "    target: t",
            "    op: add",
            "    diff_file: d",
            f"    status: {status}",
        ]
    status_path = run_dir / "status.yaml"
    status_path.write_text("\n".join(lines) + "\n")
    old = time.time() - age_days * 86400
    os.utime(status_path, (old, old))
    return run_dir


def _set_retention(project: Path, extra: str) -> None:
    cfg = project / "canonic.yaml"
    cfg.write_text(cfg.read_text() + extra)


def test_outside_project_exits_one(runner: CliRunner, outside_project) -> None:
    assert runner.invoke(app, ["gc"]).exit_code == 1


def test_nothing_to_prune_by_default(runner: CliRunner, project_dir: Path) -> None:
    _segment(project_dir, "20200101T000000000000Z")
    result = runner.invoke(app, ["gc"])
    assert result.exit_code == 0, result.output
    assert "nothing to prune" in result.output


def test_dry_run_lists_but_keeps(runner: CliRunner, project_dir: Path) -> None:
    _set_retention(project_dir, "instrumentation:\n  events:\n    max_segments: 1\n")
    old = _segment(project_dir, "20200101T000000000000Z")
    new = _segment(project_dir, "20260101T000000000000Z")

    result = runner.invoke(app, ["gc"])
    assert result.exit_code == 0, result.output
    assert "would remove" in result.output
    assert old.exists()
    assert new.exists()


def test_apply_removes_expired_segments_only(runner: CliRunner, project_dir: Path) -> None:
    _set_retention(project_dir, "instrumentation:\n  events:\n    max_segments: 1\n")
    old = _segment(project_dir, "20200101T000000000000Z")
    new = _segment(project_dir, "20260101T000000000000Z")

    result = runner.invoke(app, ["--json", "gc", "--apply"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is True
    assert [i["path"] for i in payload["event_segments"]] == [".canonic/" + old.name]
    assert not old.exists()
    assert new.exists()


def test_pending_diffs_untouched_without_flag(runner: CliRunner, project_dir: Path) -> None:
    run = _run(project_dir, "20200101T000000Z", ["accepted"], age_days=400)
    runner.invoke(app, ["gc", "--apply"])
    assert run.exists()


def test_pending_diffs_prunes_only_reviewed_old_runs(runner: CliRunner, project_dir: Path) -> None:
    old_done = _run(project_dir, "20200101T000000Z", ["accepted", "rejected"], age_days=400)
    old_open = _run(project_dir, "20200102T000000Z", ["accepted", "pending"], age_days=400)
    recent_done = _run(project_dir, "20260101T000000Z", ["accepted"], age_days=1)

    dry = runner.invoke(app, ["gc", "--pending-diffs-older-than", "30"])
    assert dry.exit_code == 0, dry.output
    assert old_done.exists()

    result = runner.invoke(app, ["gc", "--pending-diffs-older-than", "30", "--apply"])
    assert result.exit_code == 0, result.output
    assert not old_done.exists()
    assert old_open.exists()
    assert recent_done.exists()


def test_pending_run_with_unreadable_status_is_kept(runner: CliRunner, project_dir: Path) -> None:
    run = _run(project_dir, "20200101T000000Z", ["accepted"], age_days=400)
    (run / "status.yaml").write_text("proposals: [unclosed\n")
    old = time.time() - 400 * 86400
    os.utime(run / "status.yaml", (old, old))
    runner.invoke(app, ["gc", "--pending-diffs-older-than", "30", "--apply"])
    assert run.exists()
