"""Tests for outcome_evidence — the E11 pattern gate into E4 (SPEC-E11 §4, S1, S2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from canonic.config import FeedbackConfig
from canonic.contracts.loader import dump_metric_binding
from canonic.contracts.models import CanonicalRef, MetricBinding, Status
from canonic.feedback.evidence import outcome_evidence
from canonic.feedback.history import BindingOutcomeHistory
from canonic.ingestion.models import EvidenceKind
from canonic.instrumentation.models import AnswerEvent, AnswerOutcomeEvent

if TYPE_CHECKING:
    from pathlib import Path

_BASE_ANSWER: dict[str, Any] = {
    "ts": "2026-01-01T00:00:00+00:00",
    "kind": "served_answer",
    "contract_schema": "2.2",
    "query_hash": "sha256:aaa",
    "compiled_sql_hash": "sha256:bbb",
    "connection": "wh",
    "resolved": {"metrics": {"revenue": "orders.total_revenue"}},
    "guardrails_fired": [],
    "finality": None,
    "freshness": [],
    "latency_ms": 100,
    "bytes_scanned": None,
    "error": None,
    "trust_score": None,
    "cache_hit": None,
    "over_limit_blocked": None,
}

_BASE_OUTCOME: dict[str, Any] = {
    "ts": "2026-01-01T00:01:00+00:00",
    "kind": "answer_outcome",
    "ref": "sha256:aaa",
    "verdict": "incorrect",
    "reason_code": "wrong_definition",
    "correction": None,
    "marked_by": "analyst",
}


def _answer(**overrides: Any) -> AnswerEvent:
    return AnswerEvent.model_validate({**_BASE_ANSWER, **overrides})


def _outcome(**overrides: Any) -> AnswerOutcomeEvent:
    return AnswerOutcomeEvent.model_validate({**_BASE_OUTCOME, **overrides})


def _ts(days_ago: float = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _write_binding(root: Path, *, status: Status = Status.ACTIVE) -> MetricBinding:
    binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        status=status,
    )
    d = root / "contracts" / "metrics"
    d.mkdir(parents=True, exist_ok=True)
    (d / "revenue.yaml").write_text(dump_metric_binding(binding))
    return binding


def _history(
    *, count: int = 2, markers: tuple[str, ...] = ("analyst", "ci")
) -> BindingOutcomeHistory:
    answers = [_answer(query_hash=f"sha256:{i}") for i in range(count)]
    outcomes = [
        _outcome(ref=f"sha256:{i}", ts=_ts(1), marked_by=markers[i % len(markers)])
        for i in range(count)
    ]
    return BindingOutcomeHistory.from_events(answers, outcomes)


class TestPatternGate:
    def test_no_history_yields_no_evidence(self, tmp_path: Path) -> None:
        _write_binding(tmp_path)
        history = BindingOutcomeHistory.from_events([], [])
        assert outcome_evidence(tmp_path, history, FeedbackConfig()) == []

    def test_single_occurrence_below_gate_yields_no_evidence(self, tmp_path: Path) -> None:
        """S2-AC1: a single wrong_definition outcome opens a review flag at most."""
        _write_binding(tmp_path)
        history = _history(count=1, markers=("analyst",))
        assert outcome_evidence(tmp_path, history, FeedbackConfig()) == []

    def test_recurring_pattern_crosses_gate(self, tmp_path: Path) -> None:
        """S2-AC2: a recurring pattern emits contradiction evidence into E4."""
        _write_binding(tmp_path)
        history = _history(count=2, markers=("analyst", "ci"))
        items = outcome_evidence(tmp_path, history, FeedbackConfig())
        assert len(items) == 1
        item = items[0]
        assert item.kind == EvidenceKind.ANSWER_OUTCOME.value
        assert item.payload["metric"] == "revenue"
        assert item.payload["binding"] == "orders.total_revenue"
        assert item.payload["count"] == 2
        assert item.payload["refs"] == ["sha256:0", "sha256:1"]
        assert item.source == "canonic.feedback"

    def test_below_marker_threshold_yields_no_evidence(self, tmp_path: Path) -> None:
        _write_binding(tmp_path)
        history = _history(count=2, markers=("analyst",))  # same marker twice
        config = FeedbackConfig(pattern_min_count=2, pattern_min_markers=2)
        assert outcome_evidence(tmp_path, history, config) == []

    def test_no_active_binding_yields_no_evidence(self, tmp_path: Path) -> None:
        """The metric no longer exists — E11 never fabricates a target (§4)."""
        history = _history(count=2)
        assert outcome_evidence(tmp_path, history, FeedbackConfig()) == []

    def test_deprecated_binding_is_not_a_target(self, tmp_path: Path) -> None:
        _write_binding(tmp_path, status=Status.DEPRECATED)
        history = _history(count=2)
        assert outcome_evidence(tmp_path, history, FeedbackConfig()) == []


class TestAttributionQuarantine:
    """S1: only wrong_definition ever reaches evidence, regardless of volume."""

    def test_wrong_data_never_gates(self, tmp_path: Path) -> None:
        _write_binding(tmp_path)
        answers = [_answer(query_hash="sha256:0"), _answer(query_hash="sha256:1")]
        outcomes = [
            _outcome(ref="sha256:0", ts=_ts(1), reason_code="wrong_data"),
            _outcome(ref="sha256:1", ts=_ts(1), reason_code="wrong_data"),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert outcome_evidence(tmp_path, history, FeedbackConfig()) == []

    def test_wrong_interpretation_never_gates(self, tmp_path: Path) -> None:
        _write_binding(tmp_path)
        answers = [_answer(query_hash="sha256:0"), _answer(query_hash="sha256:1")]
        outcomes = [
            _outcome(ref="sha256:0", ts=_ts(1), reason_code="wrong_interpretation"),
            _outcome(ref="sha256:1", ts=_ts(1), reason_code="wrong_interpretation"),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert outcome_evidence(tmp_path, history, FeedbackConfig()) == []
