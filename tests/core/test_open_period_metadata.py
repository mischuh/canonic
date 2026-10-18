"""A served query flags a result that includes a period which has not ended."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

import canonic.core.context as context_mod
from canonic.compiler import SemanticQuery, compile
from canonic.compiler.result import TimeDimension
from canonic.config import CanonicConfig
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import QueryResult
from canonic.core.service import CanonicService
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

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
        Column(name="created_at", type="timestamp", nullable=False),
        Column(name="month_label", type="string", nullable=False),
    ],
    measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
    dimensions=[
        Dimension(name="order_month", column="created_at", granularity="month"),
        Dimension(name="order_ts", column="created_at"),
        Dimension(name="booked_month", column="month_label", time_grain="month"),
    ],
)


def _service(monkeypatch: pytest.MonkeyPatch, **trust: object) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    binding = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="total_revenue")
    )
    return CanonicService(
        config=CanonicConfig.model_validate({**_CONFIG, "trust": trust}),
        resolver=ContractResolver(bindings=[binding], guardrails=[]),
        sources=[_ORDERS],
    )


def _compile(*dimensions: str) -> CompileResult:
    binding = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="total_revenue")
    )
    return compile(
        SemanticQuery(metrics=["revenue"], dimensions=list(dimensions)),
        ContractResolver(bindings=[binding], guardrails=[]),
        [_ORDERS],
    )


def _result(column: str, value: object) -> ResultSet:
    return ResultSet(
        columns=[ResultColumn(name=column, type="date"), ResultColumn(name="revenue", type="int")],
        rows=[[value, 10]],
    )


class TestCompileReportsTimeDimensions:
    def test_bucketed_dimension_reports_its_granularity(self) -> None:
        assert _compile("order_month").time_dimensions == (TimeDimension("order_month", "month"),)

    def test_label_dimension_reports_its_time_grain(self) -> None:
        assert _compile("booked_month").time_dimensions == (TimeDimension("booked_month", "month"),)

    def test_dimension_without_a_known_grain_is_not_reported(self) -> None:
        assert _compile("order_ts").time_dimensions == ()

    def test_no_dimension_reports_nothing(self) -> None:
        assert _compile().time_dimensions == ()


class TestServedMetadata:
    def test_open_period_is_warned_and_caps_trust(self) -> None:
        compiled = _compile("order_month")

        served = QueryResult.from_parts(
            compiled, _result("order_month", datetime(2026, 10, 1)), now=NOW
        )

        assert "order_month: the month 2026-10 has not ended, so its figures are incomplete" in (
            served.metadata.warnings
        )
        assert served.metadata.trust_score is not None
        assert "order_month: month 2026-10 has not ended" in served.metadata.trust_score.reasons

    def test_label_dimension_is_flagged_too(self) -> None:
        compiled = _compile("booked_month")

        served = QueryResult.from_parts(compiled, _result("booked_month", "2026-10"), now=NOW)

        assert any("booked_month" in w for w in served.metadata.warnings)

    def test_ended_periods_add_nothing(self) -> None:
        compiled = _compile("order_month")

        served = QueryResult.from_parts(
            compiled, _result("order_month", datetime(2026, 9, 1)), now=NOW
        )

        assert not any("has not ended" in w for w in served.metadata.warnings)

    def test_compile_output_has_no_result_to_judge(self) -> None:
        from canonic.core.models import CompileOutput

        output = CompileOutput.from_compile_result(_compile("order_month"))

        assert not any("has not ended" in w for w in output.metadata.warnings)


class TestServingClock:
    def test_on_by_default_and_judged_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _service(monkeypatch)._query._serving_clock(SemanticQuery(metrics=["revenue"]))

        assert clock is not None
        assert abs((datetime.now(UTC) - clock).total_seconds()) < 60

    def test_as_of_pins_the_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pinned = datetime(2026, 3, 1, tzinfo=UTC)
        query = SemanticQuery(metrics=["revenue"], as_of=pinned)

        assert _service(monkeypatch)._query._serving_clock(query) == pinned

    def test_config_switches_the_check_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = _service(monkeypatch, flag_open_periods=False)

        assert service._query._serving_clock(SemanticQuery(metrics=["revenue"])) is None


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


class TestServedThroughTheService:
    """``query`` judges the result at the serving clock, ``as_of`` pins it."""

    @pytest.fixture
    def served(self, monkeypatch: pytest.MonkeyPatch):
        def run(query: SemanticQuery, **trust: object):
            service = _service(monkeypatch, **trust)
            result = _result("order_month", datetime(2026, 3, 1))
            monkeypatch.setattr(
                context_mod.default_factory, "for_id", lambda *a, **k: _FakeConnector(result)
            )
            return service.query(query)

        return run

    async def test_pinned_inside_the_period_is_flagged(self, served) -> None:
        query = SemanticQuery(
            metrics=["revenue"],
            dimensions=["order_month"],
            as_of=datetime(2026, 3, 15, tzinfo=UTC),
        )

        warnings = (await served(query)).metadata.warnings

        assert any("the month 2026-03 has not ended" in w for w in warnings)

    async def test_pinned_after_the_period_is_not_flagged(self, served) -> None:
        query = SemanticQuery(
            metrics=["revenue"],
            dimensions=["order_month"],
            as_of=datetime(2026, 4, 2, tzinfo=UTC),
        )

        assert not any("has not ended" in w for w in (await served(query)).metadata.warnings)

    async def test_switched_off_by_config(self, served) -> None:
        query = SemanticQuery(
            metrics=["revenue"],
            dimensions=["order_month"],
            as_of=datetime(2026, 3, 15, tzinfo=UTC),
        )

        warnings = (await served(query, flag_open_periods=False)).metadata.warnings

        assert not any("has not ended" in w for w in warnings)
