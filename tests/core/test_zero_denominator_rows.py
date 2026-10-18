"""``query`` counts the rows whose zero denominator a ratio replaced, and drops the probe."""

from __future__ import annotations

import pytest

import canonic.core.context as context_mod
from canonic.compiler import SemanticQuery
from canonic.compiler.result import PROBE_ZERO_DENOMINATOR, CompileResult
from canonic.config import CanonicConfig
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet
from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding, OnZeroDenominator
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import CompileOutput, _with_zero_denominator_counts
from canonic.core.service import CanonicService
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

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
            aliases=["sh"],
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


_GENERIC = "zero denominator for metric 'share' yields NULL (on_zero_denominator=null)"


def _flagged(count: int) -> ResultSet:
    rows = [["a", 0.1, 0], ["b", None, 1], ["c", None, 1], ["d", None, 1]][: 1 + count]
    return ResultSet(columns=_result().columns, rows=rows)


class TestWarningFollowsTheCount:
    """The warning of the ``null`` policy names the rows that need it, not the policy."""

    async def test_no_affected_row_means_no_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        served = await _service(monkeypatch, OnZeroDenominator.NULL, _flagged(0)).query(_QUERY)

        assert not any("zero denominator" in w for w in served.metadata.warnings)
        assert served.metadata.composition["share"].zero_denominator_rows == 0

    async def test_affected_rows_are_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        served = await _service(monkeypatch, OnZeroDenominator.NULL, _flagged(3)).query(_QUERY)

        assert served.metadata.warnings == [
            "zero denominator in 3 rows for metric 'share' yields NULL (on_zero_denominator=null)"
        ]

    async def test_a_single_row_is_singular(self, monkeypatch: pytest.MonkeyPatch) -> None:
        served = await _service(monkeypatch, OnZeroDenominator.NULL, _flagged(1)).query(_QUERY)

        assert served.metadata.warnings == [
            "zero denominator in 1 row for metric 'share' yields NULL (on_zero_denominator=null)"
        ]

    async def test_the_metric_is_named_by_its_canonical_name_when_asked_by_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        query = SemanticQuery(metrics=["sh"], dimensions=["category"])

        served = await _service(monkeypatch, OnZeroDenominator.NULL, _flagged(2)).query(query)

        assert served.metadata.warnings == [
            "zero denominator in 2 rows for metric 'share' yields NULL (on_zero_denominator=null)"
        ]

    def test_compile_keeps_the_generic_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        compiled = _service(monkeypatch, OnZeroDenominator.NULL).compile_query(_QUERY)

        output = CompileOutput.from_compile_result(compiled)

        assert output.metadata.warnings == [_GENERIC]

    @pytest.mark.parametrize("count", [0, 3])
    async def test_zero_policy_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch, count: int
    ) -> None:
        served = await _service(monkeypatch, OnZeroDenominator.ZERO, _flagged(count)).query(_QUERY)

        assert not any("zero denominator" in w for w in served.metadata.warnings)
        assert served.metadata.composition["share"].zero_denominator_rows == count


class TestOtherWarningsAreKept:
    def _compiled(self) -> CompileResult:
        return CompileResult(
            sql="SELECT 1",
            dialect="postgres",
            resolved={},
            warnings=["before", _GENERIC, "after"],
            zero_denominator_metrics={"sh": "share"},
        )

    def test_a_zero_count_removes_only_the_zero_denominator_warning(self) -> None:
        probes = {(PROBE_ZERO_DENOMINATOR, "sh"): 0}

        assert _with_zero_denominator_counts(self._compiled(), probes) == ["before", "after"]

    def test_a_count_replaces_the_warning_in_place(self) -> None:
        probes = {(PROBE_ZERO_DENOMINATOR, "sh"): 2}

        assert _with_zero_denominator_counts(self._compiled(), probes) == [
            "before",
            "zero denominator in 2 rows for metric 'share' yields NULL (on_zero_denominator=null)",
            "after",
        ]

    def test_without_a_count_nothing_changes(self) -> None:
        assert _with_zero_denominator_counts(self._compiled(), None) == self._compiled().warnings
        assert _with_zero_denominator_counts(self._compiled(), {}) == self._compiled().warnings
