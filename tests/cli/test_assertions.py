"""Tests for ``canonic assert`` — the accuracy harness CI gate (SPEC-Fuller-E15 §3.4, GH-110)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import canonic.cli.commands.assertions as assert_mod
from canonic.cli.app import app
from canonic.contracts.assertions import AssertionOutcome, accuracy_report

if TYPE_CHECKING:
    from typer.testing import CliRunner


class _FakeService:
    def __init__(
        self,
        outcomes: list[AssertionOutcome],
        baseline_outcomes: list[AssertionOutcome] | None = None,
    ) -> None:
        self._report = accuracy_report(outcomes)
        self._baseline_report = accuracy_report(baseline_outcomes or [])

    async def run_accuracy_harness(self) -> object:
        return self._report

    async def run_accuracy_baseline(self) -> object:
        return self._baseline_report


@pytest.fixture
def _patch_service(monkeypatch: pytest.MonkeyPatch):
    def _install(
        outcomes: list[AssertionOutcome], baseline_outcomes: list[AssertionOutcome] | None = None
    ) -> None:
        monkeypatch.setattr(
            assert_mod, "load_service", lambda ctx: _FakeService(outcomes, baseline_outcomes)
        )

    return _install


def test_all_passing_exits_zero(runner: CliRunner, _patch_service) -> None:
    _patch_service([AssertionOutcome("a", passed=True), AssertionOutcome("b", passed=True)])
    result = runner.invoke(app, ["assert"])
    assert result.exit_code == 0, result.output
    assert "100.0%" in result.output
    assert "2/2" in result.output


def test_ac2_regression_exits_ten(runner: CliRunner, _patch_service) -> None:
    _patch_service(
        [AssertionOutcome("a", passed=True), AssertionOutcome("b", passed=False, detail="b: off")]
    )
    result = runner.invoke(app, ["assert"])
    assert result.exit_code == 10
    assert "b: off" in result.output


def test_regression_json_payload_carries_assertion_id(runner: CliRunner, _patch_service) -> None:
    _patch_service([AssertionOutcome("revenue-q1", passed=False, detail="diverged")])
    result = runner.invoke(app, ["--json", "assert"])
    assert result.exit_code == 10
    report = json.loads(result.stdout)
    assert report == {
        "accuracy": 0.0,
        "passed": 0,
        "total": 1,
        "failures": [{"assertion_id": "revenue-q1", "detail": "diverged"}],
    }
    error = json.loads(result.stderr)
    assert error["code"] == "assertion_failed"
    assert error["assertion_id"] == "revenue-q1"


def test_min_accuracy_floor_allows_partial(runner: CliRunner, _patch_service) -> None:
    _patch_service(
        [AssertionOutcome("a", passed=True), AssertionOutcome("b", passed=False, detail="b: off")]
    )
    # 50% accuracy clears a 0.5 floor → no regression.
    result = runner.invoke(app, ["assert", "--min-accuracy", "0.5"])
    assert result.exit_code == 0, result.output


def test_baseline_flag_reports_lift(runner: CliRunner, _patch_service) -> None:
    _patch_service(
        [AssertionOutcome("a", passed=True), AssertionOutcome("b", passed=True)],
        [AssertionOutcome("a", passed=False, detail="a: unresolved")],
    )
    result = runner.invoke(app, ["assert", "--baseline"])
    assert result.exit_code == 0, result.output
    assert "baseline" in result.output
    assert "0.0%" in result.output
    assert "lift" in result.output
    assert "+100.0%" in result.output


def test_baseline_flag_json_payload_carries_lift(runner: CliRunner, _patch_service) -> None:
    _patch_service(
        [AssertionOutcome("a", passed=True)],
        [AssertionOutcome("a", passed=False, detail="a: unresolved")],
    )
    result = runner.invoke(app, ["--json", "assert", "--baseline"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["accuracy"] == 1.0
    assert payload["baseline"]["accuracy"] == 0.0
    assert payload["lift"] == 1.0


def test_without_baseline_flag_no_baseline_call(runner: CliRunner, _patch_service) -> None:
    _patch_service([AssertionOutcome("a", passed=True)])
    result = runner.invoke(app, ["assert"])
    assert result.exit_code == 0, result.output
    assert "baseline" not in result.output
    assert "lift" not in result.output
