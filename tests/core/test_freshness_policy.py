"""``trust.stale_after_days`` makes a long-unvalidated source stale and caps trust (SPEC-E14 §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from canonic.compiler.query import SemanticQuery
from canonic.config import CanonicConfig
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
from canonic.core.models import QueryMetadata
from canonic.core.service import CanonicService
from canonic.semantic.models import SourceMeta

if TYPE_CHECKING:
    import pytest

    from canonic.semantic.models import SemanticSource


def _service(
    source: SemanticSource,
    monkeypatch: pytest.MonkeyPatch,
    *,
    validated_days_ago: int | None,
    stale_after_days: int | None,
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    stamp = (
        None
        if validated_days_ago is None
        else datetime.now(UTC) - timedelta(days=validated_days_ago)
    )
    source = source.model_copy(update={"meta": SourceMeta(last_validated_at=stamp)})
    binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        status=Status.ACTIVE,
    )
    guardrail = Guardrail(
        id="g",
        applies_to=AppliesTo(source="orders", measure="total_revenue"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="status != 'refunded'",
        severity=Severity.ERROR,
        rationale="r",
    )
    raw: dict[str, object] = {
        "version": 1,
        "project": {"name": "test", "default_connection": "warehouse_pg"},
        "connections": [
            {
                "id": "warehouse_pg",
                "type": "postgres",
                "params": {"host": "localhost", "port": 5432, "dbname": "d", "user": "u"},
                "credentials_ref": "env:PG_PASSWORD",
            }
        ],
        "llm": {"provider": "openai_compatible", "base_url": "http://localhost/v1", "model": "m"},
    }
    if stale_after_days is not None:
        raw["trust"] = {"stale_after_days": stale_after_days}
    return CanonicService(
        config=CanonicConfig.model_validate(raw),
        resolver=ContractResolver(bindings=[binding], guardrails=[guardrail]),
        sources=[source],
    )


def _compile(service: CanonicService):
    return service.compile_query(SemanticQuery(metrics=["revenue"]))


def test_no_policy_by_default(
    orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(orders_source, monkeypatch, validated_days_ago=500, stale_after_days=None)
    assert [f.stale for f in _compile(service).freshness] == [False]


def test_old_source_is_stale_under_a_policy(
    orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(orders_source, monkeypatch, validated_days_ago=60, stale_after_days=30)
    compiled = _compile(service)
    assert [f.stale for f in compiled.freshness] == [True]
    trust = QueryMetadata.from_compile_result(compiled).trust_score
    assert trust is not None
    assert any("freshness: stale (orders)" in reason for reason in trust.reasons)


def test_recent_source_is_fresh_under_a_policy(
    orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(orders_source, monkeypatch, validated_days_ago=2, stale_after_days=30)
    compiled = _compile(service)
    assert [f.stale for f in compiled.freshness] == [False]
    trust = QueryMetadata.from_compile_result(compiled).trust_score
    assert trust is not None
    assert not any("freshness" in reason for reason in trust.reasons)


def test_describe_metric_reports_the_same_staleness(
    orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(orders_source, monkeypatch, validated_days_ago=60, stale_after_days=30)
    detail = service.describe_metric("revenue")
    assert detail.freshness is not None
    assert detail.freshness.stale is True
