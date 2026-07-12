"""Tests for E16-S1/S3/S4: AnswerEvent emitter on the serving path (issues #77, #79, #80)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from canonic.compiler.query import SemanticQuery
from canonic.config import CanonicConfig
from canonic.connectors.base import Capability, ResultSet
from canonic.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
    Status,
)
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.exc import Unresolved
from canonic.ingestion.emitter import DiskEventLog
from canonic.ingestion.models import (
    DraftedBy,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
)
from canonic.instrumentation.events import DiskAnswerEventLog
from canonic.instrumentation.models import AnswerEvent, ReconcileDecisionEvent, _sha256_json
from canonic.instrumentation.report import read_events
from canonic.semantic.models import Column, Dimension, Measure, Provenance, SemanticSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    event_log_root: Path | None = None,
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        aliases=["rev"],
        status=Status.ACTIVE,
    )
    guardrail = Guardrail(
        id="revenue-excludes-refunds",
        applies_to=AppliesTo(source="orders", measure="total_revenue"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="status != 'refunded'",
        severity=Severity.ERROR,
        rationale="Refunds are reversals, not revenue.",
    )
    source = SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[Dimension(name="status", column="status")],
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[guardrail])
    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_pg"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {
                        "host": "localhost",
                        "port": 5432,
                        "dbname": "testdb",
                        "user": "test",
                    },
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost/v1",
                "model": "llama3",
            },
        }
    )
    log_root = event_log_root if event_log_root is not None else tmp_path
    return CanonicService(
        config=config,
        resolver=resolver,
        sources=[source],
        event_log=DiskAnswerEventLog(log_root),
    )


def _fake_connector(bytes_scanned: int | None = 1024) -> Any:
    connector = MagicMock()
    connector.capabilities.return_value = [Capability.RUN_READ_ONLY_SQL]
    connector.run_read_only_sql = AsyncMock(
        return_value=ResultSet(columns=[], rows=[[6]], bytes_scanned=bytes_scanned)
    )
    connector.aclose = AsyncMock()
    return connector


def _read_events(tmp_path: Path) -> list[dict[str, Any]]:
    log_file = tmp_path / ".canonic" / "events.jsonl"
    return [json.loads(line) for line in log_file.read_text().splitlines()]


# ---------------------------------------------------------------------------
# AC1 — event written with correct shape
# ---------------------------------------------------------------------------


async def test_ac1_event_written_on_served_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)

    class _StubFactory:
        def for_id(self, _cfg, _cid):
            return _fake_connector(bytes_scanned=10485760)

    monkeypatch.setattr("canonic.core.service.default_factory", _StubFactory())

    q = SemanticQuery(metrics=["revenue"])
    await svc.query(q)

    events = _read_events(tmp_path)
    assert len(events) == 1
    ev = events[0]

    assert ev["kind"] == "served_answer"
    assert ev["query_hash"].startswith("sha256:")
    assert ev["compiled_sql_hash"].startswith("sha256:")
    assert ev["resolved"] == {"metrics": {"revenue": "orders.total_revenue"}}
    assert ev["guardrails_fired"] == ["revenue-excludes-refunds"]
    assert ev["latency_ms"] >= 0
    assert ev["bytes_scanned"] == 10485760
    assert ev["error"] is None
    assert ev["user"] is None


# ---------------------------------------------------------------------------
# Caller attribution (AMENDMENT-remote-mcp-transport.md) — the verified
# bearer-token client_id flows into the emitted answer event.
# ---------------------------------------------------------------------------


async def test_query_caller_attributed_on_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)

    class _StubFactory:
        def for_id(self, _cfg, _cid):
            return _fake_connector()

    monkeypatch.setattr("canonic.core.service.default_factory", _StubFactory())

    q = SemanticQuery(metrics=["revenue"])
    await svc.query(q, caller="alice")

    events = _read_events(tmp_path)
    assert events[0]["user"] == "alice"


async def test_run_sql_emits_event_with_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)

    class _StubFactory:
        def for_id(self, _cfg, _cid):
            return _fake_connector(bytes_scanned=2048)

    monkeypatch.setattr("canonic.core.service.default_factory", _StubFactory())

    await svc.run_sql("select 1", caller="bob")

    events = _read_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "served_answer"
    assert ev["user"] == "bob"
    assert ev["query_hash"].startswith("sha256:")
    assert ev["compiled_sql_hash"] is None
    assert ev["bytes_scanned"] == 2048
    assert ev["error"] is None


async def test_run_sql_without_caller_leaves_user_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)

    class _StubFactory:
        def for_id(self, _cfg, _cid):
            return _fake_connector()

    monkeypatch.setattr("canonic.core.service.default_factory", _StubFactory())

    await svc.run_sql("select 1")

    events = _read_events(tmp_path)
    assert events[0]["user"] is None


# ---------------------------------------------------------------------------
# AC2 — content safety: no SQL text, no rows, reserved fields null
# ---------------------------------------------------------------------------


async def test_ac2_log_contains_no_sql_or_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)

    class _StubFactory:
        def for_id(self, _cfg, _cid):
            return _fake_connector()

    monkeypatch.setattr("canonic.core.service.default_factory", _StubFactory())

    q = SemanticQuery(metrics=["revenue"])
    await svc.query(q)

    raw_line = (tmp_path / ".canonic" / "events.jsonl").read_text()

    # No SQL text in the log
    assert "SELECT" not in raw_line.upper()
    assert "sum(" not in raw_line.lower()
    # No literal row values
    assert "[6]" not in raw_line
    # No filter literals (the guardrail filter contains "'refunded'")
    assert "refunded" not in raw_line

    ev = json.loads(raw_line)
    # trust_score is populated from E14 (SPEC-E16 Part 2 §4); cache_hit/over_limit_blocked
    # remain reserved until E13 lands (S3-AC1).
    assert ev["trust_score"] in ("caution", "provisional", "trusted")
    assert ev["cache_hit"] is None
    assert ev["over_limit_blocked"] is None


# ---------------------------------------------------------------------------
# S3 — round-trip validation of reserved fields
# ---------------------------------------------------------------------------


_SNAPSHOTS = Path(__file__).parent.parent / "snapshots" / "contract_schema_v1"


def test_answer_event_schema_unchanged() -> None:
    golden = json.loads((_SNAPSHOTS / "answer_event.json").read_text())
    assert AnswerEvent.model_json_schema() == golden, (
        "AnswerEvent schema changed — update tests/snapshots/contract_schema_v1/answer_event.json "
        "and bump contract_schema per SPEC-P0 §4"
    )


def test_reconcile_decision_event_schema_unchanged() -> None:
    golden = json.loads((_SNAPSHOTS / "reconcile_decision_event.json").read_text())
    assert ReconcileDecisionEvent.model_json_schema() == golden, (
        "ReconcileDecisionEvent schema changed — update "
        "tests/snapshots/contract_schema_v1/reconcile_decision_event.json per SPEC-P0 §4"
    )


def test_s3_reserved_fields_present_and_null() -> None:
    ev = AnswerEvent(
        ts="2026-06-15T12:00:00+00:00",
        contract_schema="1.5",
        query_hash="sha256:abc",
        compiled_sql_hash="sha256:def",
        connection="warehouse_pg",
        latency_ms=42,
    )
    dumped = ev.model_dump(mode="json")
    assert dumped["trust_score"] is None
    assert dumped["cache_hit"] is None
    assert dumped["over_limit_blocked"] is None
    # Round-trip
    reloaded = AnswerEvent.model_validate(dumped)
    assert reloaded == ev


def test_s3_populated_reserved_fields_validate() -> None:
    """AC2: populated values validate against the same v1 shape (no migration needed)."""
    ev = AnswerEvent(
        ts="2026-06-15T12:00:00+00:00",
        contract_schema="1.5",
        query_hash="sha256:abc",
        compiled_sql_hash="sha256:def",
        connection="warehouse_pg",
        latency_ms=42,
        trust_score="trusted",
        cache_hit=True,
        over_limit_blocked=False,
    )
    dumped = ev.model_dump(mode="json")
    assert dumped["trust_score"] == "trusted"
    assert dumped["cache_hit"] is True
    assert dumped["over_limit_blocked"] is False
    reloaded = AnswerEvent.model_validate(dumped)
    assert reloaded == ev


# ---------------------------------------------------------------------------
# Failure path — error is logged, exception still propagates
# ---------------------------------------------------------------------------


async def test_failure_event_written_and_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)

    q = SemanticQuery(metrics=["nonexistent_metric"])
    with pytest.raises(Unresolved):
        await svc.query(q)

    events = _read_events(tmp_path)
    assert len(events) == 1
    ev = events[0]

    assert ev["kind"] == "served_answer"
    assert ev["error"] == "unresolved"
    assert ev["compiled_sql_hash"] is None
    assert ev["bytes_scanned"] is None


# ---------------------------------------------------------------------------
# Append-only — two queries → two lines
# ---------------------------------------------------------------------------


async def test_append_only_two_queries_two_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)

    class _StubFactory:
        def for_id(self, _cfg, _cid):
            return _fake_connector()

    monkeypatch.setattr("canonic.core.service.default_factory", _StubFactory())

    q = SemanticQuery(metrics=["revenue"])
    await svc.query(q)
    await svc.query(q)

    events = _read_events(tmp_path)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# NullAnswerEventLog — no file written when no event log configured
# ---------------------------------------------------------------------------


async def test_null_event_log_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        status=Status.ACTIVE,
    )
    source = SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[],
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[])
    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_pg"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {
                        "host": "localhost",
                        "port": 5432,
                        "dbname": "testdb",
                        "user": "test",
                    },
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost/v1",
                "model": "llama3",
            },
        }
    )
    # No event_log — defaults to NullAnswerEventLog
    svc = CanonicService(config=config, resolver=resolver, sources=[source])

    class _StubFactory:
        def for_id(self, _cfg, _cid):
            return _fake_connector()

    monkeypatch.setattr("canonic.core.service.default_factory", _StubFactory())

    await svc.query(SemanticQuery(metrics=["revenue"]))

    assert not (tmp_path / ".canonic" / "events.jsonl").exists()


# ---------------------------------------------------------------------------
# sha256 helper
# ---------------------------------------------------------------------------


def test_sha256_json_format() -> None:
    result = _sha256_json({"key": "value"})
    assert result.startswith("sha256:")
    assert len(result) == len("sha256:") + 64  # hex digest is 64 chars


# ---------------------------------------------------------------------------
# E16-S4 AC1 — both kinds land in one file, filterable by kind
# ---------------------------------------------------------------------------


def _reconcile_entry() -> ReconciliationEntry:
    proposal = Proposal(
        target="semantics/warehouse_pg/orders.yaml",
        op=ProposalOp.ADD,
        content={"name": "orders"},
        provenance=Provenance.INFERRED,
        confidence=0.9,
        anchored_to=["sha256:abc"],
        drafted_by=DraftedBy.DETERMINISTIC,
    )
    return ReconciliationEntry(
        decision=ReconciliationDecision.ADD, target=proposal.target, proposal=proposal
    )


def _answer_event() -> AnswerEvent:
    return AnswerEvent(
        ts="2026-06-19T12:00:00+00:00",
        contract_schema="1.5",
        query_hash="sha256:aaa",
        compiled_sql_hash="sha256:bbb",
        connection="warehouse_pg",
        latency_ms=50,
    )


def test_s4_both_kinds_in_one_file(tmp_path: Path) -> None:
    """AC1 — served_answer and reconcile_decision both land in .canonic/events.jsonl."""
    DiskAnswerEventLog(tmp_path).append(_answer_event())
    DiskEventLog(tmp_path).append([_reconcile_entry()], run_id="test-run-id")

    log_path = tmp_path / ".canonic" / "events.jsonl"
    assert log_path.exists()
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    kinds = {json.loads(line)["kind"] for line in lines}
    assert kinds == {"served_answer", "reconcile_decision"}


def test_s4_read_events_returns_both_kinds(tmp_path: Path) -> None:
    """AC1 — read_events() returns both kinds from the same file."""
    DiskAnswerEventLog(tmp_path).append(_answer_event())
    DiskEventLog(tmp_path).append([_reconcile_entry()], run_id="test-run-id")

    all_events = read_events(tmp_path)
    assert len(all_events) == 2
    assert {e.kind for e in all_events} == {"served_answer", "reconcile_decision"}


def test_s4_read_events_kind_filter(tmp_path: Path) -> None:
    """AC1 — read_events(kind=...) filters to one event type."""
    DiskAnswerEventLog(tmp_path).append(_answer_event())
    DiskEventLog(tmp_path).append([_reconcile_entry()], run_id="test-run-id")

    served = read_events(tmp_path, kind="served_answer")
    assert len(served) == 1
    assert all(e.kind == "served_answer" for e in served)

    reconcile = read_events(tmp_path, kind="reconcile_decision")
    assert len(reconcile) == 1
    assert all(e.kind == "reconcile_decision" for e in reconcile)


def test_s5_logging_works_under_air_gapped(tmp_path: Path) -> None:
    """AC1 (GH-81) — local event logging is unaffected when runtime.air_gapped is true.

    The event log is pure local I/O; the air-gapped flag only blocks telemetry egress,
    not the write to .canonic/events.jsonl.
    """
    log = DiskAnswerEventLog(tmp_path)
    log.append(_answer_event())

    log_path = tmp_path / ".canonic" / "events.jsonl"
    assert log_path.exists(), "events.jsonl must be written regardless of air-gapped mode"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "served_answer"
