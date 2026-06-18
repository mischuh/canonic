"""Root-level fixtures shared across all test suites."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import litellm
import pytest

from canon.config import CanonConfig
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
from canon.semantic.models import Column, Dimension, Measure, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable


def _response(content: str) -> SimpleNamespace:
    """Minimal litellm ModelResponse stand-in exposing the content path used by the drafter."""
    message = SimpleNamespace(content=content)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``litellm.acompletion`` with a recording fake.

    Returns the captured-kwargs dict. The canned content defaults to a valid grain payload;
    a test can override behaviour with :func:`set_fake`.
    """
    captured: dict[str, Any] = {}
    state: dict[str, Any] = {"content": '{"grain": ["id"]}', "raises": None, "raises_times": 0}
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        calls.append(dict(kwargs))
        if state["raises"] is not None and (
            state["raises_times"] == 0 or len(calls) <= state["raises_times"]
        ):
            raise state["raises"]
        return _response(state["content"])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    captured["_state"] = state
    captured["_calls"] = calls
    return captured


@pytest.fixture
def set_fake(fake_litellm: dict[str, Any]) -> Callable[..., None]:
    """Helper to set the fake's canned content or raised exception."""

    def _set(
        *,
        content: str | None = None,
        raises: BaseException | None = None,
        raises_times: int = 0,
    ) -> None:
        if content is not None:
            fake_litellm["_state"]["content"] = content
        fake_litellm["_state"]["raises"] = raises
        fake_litellm["_state"]["raises_times"] = raises_times

    return _set


@pytest.fixture
def orders_source() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[
            Dimension(name="order_date", column="created_at"),
            Dimension(name="status", column="status"),
        ],
    )


@pytest.fixture
def canon_service(orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch) -> CanonService:
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
    return CanonService(config=config, resolver=resolver, sources=[orders_source])
