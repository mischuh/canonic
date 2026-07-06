"""Tests for canonic/cli/_errors.py — structured error emission (adapter parity, SPEC §2.1)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canonic.cli._errors import emit_error
from canonic.compiler.joins import JoinPathCandidate
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.exc import Ambiguous, AmbiguousJoinPath, CanonicError

if TYPE_CHECKING:
    import pytest


def test_emit_error_json_includes_string_candidates(capsys: pytest.CaptureFixture[str]) -> None:
    err = Ambiguous(
        "dimension 'country' is ambiguous",
        candidates=["customers.country", "pickup.country"],
    )
    emit_error(err, json_output=True)
    payload = json.loads(capsys.readouterr().err)
    assert payload["candidates"] == ["customers.country", "pickup.country"]


def test_emit_error_text_lists_string_candidates(capsys: pytest.CaptureFixture[str]) -> None:
    err = Ambiguous(
        "dimension 'country' is ambiguous",
        candidates=["customers.country", "pickup.country"],
    )
    emit_error(err, json_output=False)
    out = capsys.readouterr().err
    assert "candidate 1: customers.country" in out
    assert "candidate 2: pickup.country" in out
    assert "hint: qualify with one of the candidates above" in out
    assert "customers.country" in out.split("hint:")[1]


def test_emit_error_json_includes_metric_binding_candidates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = MetricBinding(metric="revenue", canonical=CanonicalRef(source="orders", measure="total"))
    b = MetricBinding(metric="rev", canonical=CanonicalRef(source="orders2", measure="total2"))
    err = Ambiguous("metric 'revenue' is ambiguous", candidates=[a, b])
    emit_error(err, json_output=True)
    payload = json.loads(capsys.readouterr().err)
    assert payload["candidates"] == [a.model_dump(mode="json"), b.model_dump(mode="json")]


def test_emit_error_text_lists_metric_binding_candidates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = MetricBinding(metric="revenue", canonical=CanonicalRef(source="orders", measure="total"))
    err = Ambiguous("metric 'revenue' is ambiguous", candidates=[a])
    emit_error(err, json_output=False)
    out = capsys.readouterr().err
    assert "candidate 1: revenue" in out


def test_emit_error_join_path_candidates_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: route/via rendering for AmbiguousJoinPath must not change."""
    candidate = JoinPathCandidate(via=["a", "c"], route="o → a → c", joins=[])
    err = AmbiguousJoinPath("ambiguous join path", owner="o", target="c", candidates=[candidate])
    emit_error(err, json_output=False)
    out = capsys.readouterr().err
    assert "path 1: o → a → c" in out
    assert 'hint: re-issue with "via"' in out


def test_emit_error_via_list_candidates_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: list/tuple-of-aliases rendering must not change."""
    err = Ambiguous("ambiguous source", candidates=[["a", "c"], ["b", "c"]])
    emit_error(err, json_output=False)
    out = capsys.readouterr().err
    assert "a → c" in out
    assert "b → c" in out
    assert 'hint: add "via"' in out


def test_emit_error_no_candidates_omits_key(capsys: pytest.CaptureFixture[str]) -> None:
    err = CanonicError("boom")
    emit_error(err, json_output=True)
    payload = json.loads(capsys.readouterr().err)
    assert "candidates" not in payload
