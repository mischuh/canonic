"""Tests for ``canonic outcome mark`` — outcome capture with attribution (SPEC-E16 Part 2 §3)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canonic.cli.app import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner


def test_outside_project_exits_one(runner: CliRunner, outside_project) -> None:
    result = runner.invoke(app, ["outcome", "mark", "--ref", "sha256:x", "--verdict", "correct"])
    assert result.exit_code == 1


def test_marks_correct(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["outcome", "mark", "--ref", "sha256:abc", "--verdict", "correct"])
    assert result.exit_code == 0, result.output

    events = (project_dir / ".canonic" / "events.jsonl").read_text().splitlines()
    assert len(events) == 1
    ev = json.loads(events[0])
    assert ev["kind"] == "answer_outcome"
    assert ev["ref"] == "sha256:abc"
    assert ev["verdict"] == "correct"
    assert ev["reason_code"] is None
    assert ev["marked_by"] == "analyst"


def test_marks_incorrect_with_reason(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "outcome",
            "mark",
            "--ref",
            "sha256:abc",
            "--verdict",
            "incorrect",
            "--reason",
            "wrong_definition",
            "--by",
            "ci",
            "--correction",
            "sha256:fixed",
        ],
    )
    assert result.exit_code == 0, result.output
    ev = json.loads((project_dir / ".canonic" / "events.jsonl").read_text().splitlines()[0])
    assert ev["verdict"] == "incorrect"
    assert ev["reason_code"] == "wrong_definition"
    assert ev["marked_by"] == "ci"
    assert ev["correction"] == "sha256:fixed"


def test_incorrect_without_reason_defaults_to_unspecified(
    runner: CliRunner, project_dir: Path
) -> None:
    result = runner.invoke(
        app, ["outcome", "mark", "--ref", "sha256:abc", "--verdict", "incorrect"]
    )
    assert result.exit_code == 0, result.output
    ev = json.loads((project_dir / ".canonic" / "events.jsonl").read_text().splitlines()[0])
    assert ev["reason_code"] == "unspecified"


def test_correct_with_reason_exits_nine(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "outcome",
            "mark",
            "--ref",
            "sha256:abc",
            "--verdict",
            "correct",
            "--reason",
            "wrong_data",
        ],
    )
    assert result.exit_code == 9


def test_unknown_ref_warns_but_still_records(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(
        app, ["outcome", "mark", "--ref", "sha256:nonexistent", "--verdict", "correct"]
    )
    assert result.exit_code == 0
    assert "does not match any recorded AnswerEvent" in result.output
    events = (project_dir / ".canonic" / "events.jsonl").read_text().splitlines()
    assert len(events) == 1


def test_json_output(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(
        app, ["--json", "outcome", "mark", "--ref", "sha256:abc", "--verdict", "correct"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "answer_outcome"
    assert payload["ref"] == "sha256:abc"
