"""Tests for AnswerOutcomeEvent — the FR-9 ground-truth feed (SPEC-E16 Part 2 §3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from canonic.instrumentation.events import DiskAnswerEventLog
from canonic.instrumentation.models import (
    AnswerOutcomeEvent,
    OutcomeReasonCode,
    OutcomeVerdict,
)
from canonic.instrumentation.report import read_events

_SNAPSHOTS = Path(__file__).parent.parent / "snapshots" / "contract_schema_v1"


def _base(**overrides: Any) -> dict[str, Any]:
    base = {
        "ts": "2026-06-15T12:00:00+00:00",
        "ref": "sha256:aaa",
        "verdict": "correct",
        "marked_by": "analyst",
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# S2-AC1 — attribution: reason_code links wrong_definition to the binding
# ---------------------------------------------------------------------------


def test_incorrect_with_wrong_definition_carries_reason_code() -> None:
    ev = AnswerOutcomeEvent.model_validate(
        _base(verdict="incorrect", reason_code="wrong_definition")
    )
    assert ev.verdict is OutcomeVerdict.INCORRECT
    assert ev.reason_code is OutcomeReasonCode.WRONG_DEFINITION


def test_incorrect_without_reason_code_defaults_to_unspecified() -> None:
    """SPEC-E16 Part 2 §9 — conservative default when the analyst doesn't specify a cause."""
    ev = AnswerOutcomeEvent.model_validate(_base(verdict="incorrect"))
    assert ev.reason_code is OutcomeReasonCode.UNSPECIFIED


# ---------------------------------------------------------------------------
# S2-AC2 — wrong_data / wrong_interpretation are recorded but distinct from wrong_definition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["wrong_data", "wrong_interpretation", "wrong_definition", "unspecified"]
)
def test_all_reason_codes_validate(reason: str) -> None:
    ev = AnswerOutcomeEvent.model_validate(_base(verdict="incorrect", reason_code=reason))
    assert ev.reason_code == reason


def test_correct_verdict_rejects_reason_code() -> None:
    with pytest.raises(ValidationError, match="reason_code must be omitted"):
        AnswerOutcomeEvent.model_validate(_base(verdict="correct", reason_code="wrong_data"))


def test_correct_verdict_without_reason_code() -> None:
    ev = AnswerOutcomeEvent.model_validate(_base(verdict="correct"))
    assert ev.reason_code is None


# ---------------------------------------------------------------------------
# marked_by / correction / ref
# ---------------------------------------------------------------------------


def test_marked_by_roles() -> None:
    for role in ("analyst", "agent", "ci"):
        ev = AnswerOutcomeEvent.model_validate(_base(marked_by=role))
        assert ev.marked_by == role


def test_correction_optional() -> None:
    ev = AnswerOutcomeEvent.model_validate(
        _base(verdict="incorrect", reason_code="wrong_definition", correction="sha256:fixed")
    )
    assert ev.correction == "sha256:fixed"


def test_ref_links_to_answer_event_query_hash() -> None:
    ev = AnswerOutcomeEvent.model_validate(_base(ref="sha256:the-answer"))
    assert ev.ref == "sha256:the-answer"


# ---------------------------------------------------------------------------
# NDJSON round-trip via the event log substrate
# ---------------------------------------------------------------------------


def test_append_and_read_back(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path)
    ev = AnswerOutcomeEvent.model_validate(
        _base(verdict="incorrect", reason_code="wrong_definition", correction="sha256:fixed")
    )
    log.append(ev)

    raw = (tmp_path / ".canonic" / "events.jsonl").read_text()
    assert json.loads(raw)["kind"] == "answer_outcome"

    events = read_events(tmp_path, kind="answer_outcome")
    assert events == [ev]


def test_content_safety_no_extra_fields(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path)
    ev = AnswerOutcomeEvent.model_validate(_base(verdict="correct"))
    log.append(ev)
    raw = json.loads((tmp_path / ".canonic" / "events.jsonl").read_text())
    assert set(raw) == {"ts", "kind", "ref", "verdict", "reason_code", "correction", "marked_by"}


# ---------------------------------------------------------------------------
# Frozen schema (SPEC-P0 §4/§5)
# ---------------------------------------------------------------------------


def test_answer_outcome_event_schema_unchanged() -> None:
    golden = json.loads((_SNAPSHOTS / "answer_outcome_event.json").read_text())
    assert AnswerOutcomeEvent.model_json_schema() == golden, (
        "AnswerOutcomeEvent schema changed — update "
        "tests/snapshots/contract_schema_v1/answer_outcome_event.json and bump contract_schema "
        "per SPEC-P0 §4"
    )
