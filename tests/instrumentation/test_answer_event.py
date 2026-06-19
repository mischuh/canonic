"""Tests for E16-S1: AnswerEvent emitter on the serving path (issue #77)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from canon.compiler.query import SemanticQuery
from canon.config import CanonConfig
from canon.connectors.base import ResultSet
from canon.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
    Status,
)
from canon.contracts.resolver import ContractResolver
from canon.core.service import CanonService
from canon.exc import Unresolved
from canon.instrumentation.events import DiskAnswerEventLog
from canon.instrumentation.models import AnswerEvent, _sha256_json
from canon.semantic.models import Column, Dimension, Measure, SemanticSource

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    event_log_root: Path | None = None,
) -> CanonService:
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
    config = CanonConfig.model_validate(
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
    return CanonService(
        config=config,
        resolver=resolver,
        sources=[source],
        event_log=DiskAnswerEventLog(log_root),
    )


def _fake_connector(bytes_scanned: int | None = 1024) -> Any:
    connector = MagicMock()
    connector.run_read_only_sql = AsyncMock(
        return_value=ResultSet(columns=[], rows=[[6]], bytes_scanned=bytes_scanned)
    )
    connector.aclose = AsyncMock()
    return connector


def _read_events(tmp_path: Path) -> list[dict[str, Any]]:
    log_file = tmp_path / ".canon" / "events.jsonl"
    return [json.loads(line) for line in log_file.read_text().splitlines()]


# ---------------------------------------------------------------------------
# AC1 — event written with correct shape
# ---------------------------------------------------------------------------


async def test_ac1_event_written_on_served_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "canon.core.service.connector_by_id",
        lambda _cfg, _cid: _fake_connector(bytes_scanned=10485760),
    )

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


# ---------------------------------------------------------------------------
# AC2 — content safety: no SQL text, no rows, reserved fields null
# ---------------------------------------------------------------------------


async def test_ac2_log_contains_no_sql_or_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _make_service(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "canon.core.service.connector_by_id",
        lambda _cfg, _cid: _fake_connector(),
    )

    q = SemanticQuery(metrics=["revenue"])
    await svc.query(q)

    raw_line = (tmp_path / ".canon" / "events.jsonl").read_text()

    # No SQL text in the log
    assert "SELECT" not in raw_line.upper()
    assert "sum(" not in raw_line.lower()
    # No literal row values
    assert "[6]" not in raw_line
    # No filter literals (the guardrail filter contains "'refunded'")
    assert "refunded" not in raw_line

    ev = json.loads(raw_line)
    # Reserved fields serialise as null (S3-AC1)
    assert ev["trust_score"] is None
    assert ev["cache_hit"] is None
    assert ev["over_limit_blocked"] is None


# ---------------------------------------------------------------------------
# S3 — round-trip validation of reserved fields
# ---------------------------------------------------------------------------


def test_s3_reserved_fields_present_and_null() -> None:
    ev = AnswerEvent(
        ts="2026-06-15T12:00:00+00:00",
        contract_schema="1.1",
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
    monkeypatch.setattr(
        "canon.core.service.connector_by_id",
        lambda _cfg, _cid: _fake_connector(),
    )

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
    config = CanonConfig.model_validate(
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
    svc = CanonService(config=config, resolver=resolver, sources=[source])
    monkeypatch.setattr(
        "canon.core.service.connector_by_id",
        lambda _cfg, _cid: _fake_connector(),
    )

    await svc.query(SemanticQuery(metrics=["revenue"]))

    assert not (tmp_path / ".canon" / "events.jsonl").exists()


# ---------------------------------------------------------------------------
# sha256 helper
# ---------------------------------------------------------------------------


def test_sha256_json_format() -> None:
    result = _sha256_json({"key": "value"})
    assert result.startswith("sha256:")
    assert len(result) == len("sha256:") + 64  # hex digest is 64 chars
