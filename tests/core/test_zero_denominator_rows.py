"""``query`` counts the rows whose zero denominator a ratio replaced, and drops the probe."""

from __future__ import annotations

from typing import TYPE_CHECKING

import canonic.core.context as context_mod
from canonic.compiler import SemanticQuery
from canonic.config import CanonicConfig
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet
from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding, OnZeroDenominator
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import CompileOutput
from canonic.core.service import CanonicService
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

if TYPE_CHECKING:
    import pytest

_CONFIG = {
    "version": 1,
    "project": {"name": "test", "default_connection": "warehouse_pg"},
    "connections": [
        {
            "id": "warehouse_pg",
            "type": "postgres",
            "params": {"host": "localhost", "port": 5432, "dbname": "testdb", "user": "test"},
            "credentials_ref": "env:PG_PASSWORD",
        }
    ],
    "llm": {"provider": "openai_compatible", "base_url": "http://localhost/v1", "model": "llama3"},
}

_ORDERS = SemanticSource(
    name="orders",
    connection="warehouse_pg",
    table="analytics.fct_orders",
    grain=["order_id"],
    columns=[
        Column(name="order_id", type="string", nullable=False),
        Column(name="amount", type="decimal", nullable=False),
        Column(name="shipping", type="decimal", nullable=False),
        Column(name="category", type="string", nullable=False),
    ],
    measures=[
        Measure(name="total_amount", expr="sum(amount)", additivity="additive"),
        Measure(name="total_shipping", expr="sum(shipping)", additivity="additive"),
    ],
    dimensions=[Dimension(name="category", column="category")],
)

_PROBE = "_canonic_zero_denominator_share"


class _FakeConnector(ConnectorBase):
    def __init__(self, result: ResultSet) -> None:
        self._result = result

    def capabilities(self) -> list[Capability]:
        return [Capability.RUN_READ_ONLY_SQL]

    async def test_connection(self) -> Health:  # pragma: no cover - unused
        return Health(status="ok")

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        return self._result

    async def aclose(self) -> None:
        return None


def _service(
    monkeypatch: pytest.MonkeyPatch, on_zero: OnZeroDenominator, result: ResultSet | None = None
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    if result is not None:
        monkeypatch.setattr(
            context_mod.default_factory, "for_id", lambda *a, **k: _FakeConnector(result)
        )
    bindings = [
        MetricBinding(
            metric="amount", canonical=CanonicalRef(source="orders", measure="total_amount")
        ),
        MetricBinding(
            metric="shipping", canonical=CanonicalRef(source="orders", measure="total_shipping")
        ),
        MetricBinding(
            metric="share",
            canonical=CanonicalRef(
                kind=BindingKind.RATIO,
                numerator="shipping",
                denominator="amount",
                on_zero_denominator=on_zero,
            ),
        ),
    ]
    return CanonicService(
        config=CanonicConfig.model_validate(_CONFIG),
        resolver=ContractResolver(bindings=bindings, guardrails=[]),
        sources=[_ORDERS],
    )


def _result() -> ResultSet:
    return ResultSet(
        columns=[
            ResultColumn(name="category", type="string"),
            ResultColumn(name="share", type="float"),
            ResultColumn(name=_PROBE, type="int"),
        ],
        rows=[["a", 0.1, 0], ["b", 0, 1], ["c", 0, 1], ["d", 0.5, 0]],
    )


_QUERY = SemanticQuery(metrics=["share"], dimensions=["category"])


async def test_query_reports_how_many_rows_were_substituted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = await _service(monkeypatch, OnZeroDenominator.ZERO, _result()).query(_QUERY)

    composition = served.metadata.composition["share"]
    assert composition.on_zero_denominator == "zero"
    assert composition.zero_denominator_rows == 2


async def test_probe_column_never_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    served = await _service(monkeypatch, OnZeroDenominator.ZERO, _result()).query(_QUERY)

    assert [c.name for c in served.result.columns] == ["category", "share"]
    assert served.result.rows == [["a", 0.1], ["b", 0], ["c", 0], ["d", 0.5]]


async def test_no_zero_denominator_counts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    clean = ResultSet(columns=_result().columns, rows=[["a", 0.1, 0], ["d", 0.5, 0]])

    served = await _service(monkeypatch, OnZeroDenominator.NULL, clean).query(_QUERY)

    assert served.metadata.composition["share"].zero_denominator_rows == 0


async def test_error_policy_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = ResultSet(columns=_result().columns[:2], rows=[["a", 0.1]])

    served = await _service(monkeypatch, OnZeroDenominator.ERROR, plain).query(_QUERY)

    assert served.metadata.composition["share"].zero_denominator_rows is None


def test_compile_output_has_no_result_to_count(monkeypatch: pytest.MonkeyPatch) -> None:
    compiled = _service(monkeypatch, OnZeroDenominator.ZERO).compile_query(_QUERY)

    output = CompileOutput.from_compile_result(compiled)

    assert output.metadata.composition["share"].zero_denominator_rows is None
    assert _PROBE in output.compiled.sql
