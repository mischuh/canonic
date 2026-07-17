"""Service-layer assertion execution + harness gating (SPEC-Fuller-E15 §3, GH-109)."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

import canonic.core.context as context_mod
from canonic.compiler.query import SemanticQuery
from canonic.config import CanonicConfig
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet
from canonic.contracts.models import Assertion, AssertionExpect, CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.exc import AssertionFailed

if TYPE_CHECKING:
    from canonic.semantic.models import SemanticSource


class _FakeConnector(ConnectorBase):
    """A read-only connector that returns a canned result for every query."""

    def __init__(self, result: ResultSet) -> None:
        self._result = result
        self.closed = False

    def capabilities(self) -> list[Capability]:
        return [Capability.RUN_READ_ONLY_SQL]

    async def test_connection(self) -> Health:  # pragma: no cover — unused
        return Health(status="ok")

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        return self._result

    async def aclose(self) -> None:
        self.closed = True


def _config() -> CanonicConfig:
    return CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_pg"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {"host": "h", "port": 5432, "dbname": "d", "user": "u"},
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )


def _service(
    orders_source: SemanticSource,
    assertions: list[Assertion],
    result: ResultSet,
    monkeypatch: pytest.MonkeyPatch,
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    monkeypatch.setattr(
        context_mod.default_factory, "for_id", lambda *a, **k: _FakeConnector(result)
    )
    resolver = ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue",
                canonical=CanonicalRef(source="orders", measure="total_revenue"),
            )
        ],
        guardrails=[],
        assertions=assertions,
    )
    return CanonicService(config=_config(), resolver=resolver, sources=[orders_source])


def _revenue_result(value: object) -> ResultSet:
    return ResultSet(columns=[ResultColumn(name="total_revenue", type="decimal")], rows=[[value]])


def _assertion(value: float, *, tolerance: float | None = None) -> Assertion:
    return Assertion(
        id="revenue-2025-q1",
        query={"metrics": ["revenue"]},
        expect=AssertionExpect(rows=1, values={"total_revenue": value}, tolerance=tolerance),
        source_of_truth="Finance close",
    )


class TestRunAssertion:
    async def test_passes_when_result_matches(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _service(
            orders_source, [_assertion(100.0)], _revenue_result(Decimal("100.0")), monkeypatch
        )
        outcome = await svc.run_assertion(_assertion(100.0))
        assert outcome.passed

    async def test_fails_with_diff_when_result_diverges(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _service(
            orders_source, [_assertion(4218334.10)], _revenue_result(4100000), monkeypatch
        )
        outcome = await svc.run_assertion(_assertion(4218334.10))
        assert not outcome.passed
        assert "revenue-2025-q1" in outcome.detail


class TestCheckAssertions:
    async def test_runs_all_loaded_executable_assertions(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = Assertion(id="usage-x", query={"native": "sum(amount)"})
        svc = _service(
            orders_source,
            [_assertion(100.0), candidate],
            _revenue_result(Decimal("100.0")),
            monkeypatch,
        )
        outcomes = await svc.check_assertions()
        assert [o.assertion_id for o in outcomes] == ["revenue-2025-q1"]
        assert outcomes[0].passed


class TestAccuracyHarness:
    async def test_ac1_yields_accuracy_number(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One matching, one diverging assertion → 1/2 = 0.5 against the canned result.
        passing = _assertion(100.0)
        failing = Assertion(
            id="revenue-2024",
            query={"metrics": ["revenue"]},
            expect=AssertionExpect(rows=1, values={"total_revenue": 999.0}),
        )
        svc = _service(
            orders_source, [passing, failing], _revenue_result(Decimal("100.0")), monkeypatch
        )
        report = await svc.run_accuracy_harness()
        assert report.total == 2
        assert report.passed == 1
        assert report.accuracy == 0.5
        assert [o.assertion_id for o in report.failures] == ["revenue-2024"]

    async def test_deterministic_order_independent_of_pass_fail(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Candidate (non-executable) assertions are skipped, not counted as failures.
        candidate = Assertion(id="usage-x", query={"native": "sum(amount)"})
        svc = _service(
            orders_source,
            [_assertion(100.0), candidate],
            _revenue_result(Decimal("100.0")),
            monkeypatch,
        )
        report = await svc.run_accuracy_harness()
        assert report.total == 1
        assert report.accuracy == 1.0


class TestAccuracyBaseline:
    async def test_lift_is_measurable_over_schema_only_resolver(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "revenue" only resolves via the curated binding; the raw measure is
        # "total_revenue", so the schema-only baseline can't resolve it (SPEC-E16 P2 §2).
        svc = _service(
            orders_source, [_assertion(100.0)], _revenue_result(Decimal("100.0")), monkeypatch
        )
        canon = await svc.run_accuracy_harness()
        baseline = await svc.run_accuracy_baseline()
        assert canon.accuracy == 1.0
        assert baseline.accuracy == 0.0
        assert baseline.total == 1
        assert "revenue-2025-q1" in baseline.failures[0].assertion_id
        assert canon.accuracy - baseline.accuracy > 0

    async def test_baseline_resolves_metrics_named_after_raw_measures(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A query naming the raw measure directly ("total_revenue") resolves fine
        # under the schema-only resolver — it's curated *aliases* that don't survive.
        raw_assertion = Assertion(
            id="raw-total-revenue",
            query={"metrics": ["total_revenue"]},
            expect=AssertionExpect(rows=1, values={"total_revenue": 100.0}),
        )
        svc = _service(
            orders_source, [raw_assertion], _revenue_result(Decimal("100.0")), monkeypatch
        )
        baseline = await svc.run_accuracy_baseline([raw_assertion])
        assert baseline.accuracy == 1.0

    async def test_candidate_assertions_are_skipped_not_failed(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = Assertion(id="usage-x", query={"native": "sum(amount)"})
        svc = _service(
            orders_source, [_assertion(100.0), candidate], _revenue_result(100.0), monkeypatch
        )
        baseline = await svc.run_accuracy_baseline()
        assert baseline.total == 1


class TestHarnessGate:
    async def test_ac1_harness_mode_raises_assertion_failed_on_mismatch(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _service(
            orders_source, [_assertion(4218334.10)], _revenue_result(4100000), monkeypatch
        )
        with pytest.raises(AssertionFailed) as excinfo:
            await svc.query(SemanticQuery(metrics=["revenue"]), harness=True)
        assert excinfo.value.assertion_id == "revenue-2025-q1"
        assert excinfo.value.exit_code == 10

    async def test_harness_mode_passes_when_matching(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _service(
            orders_source, [_assertion(100.0)], _revenue_result(Decimal("100.0")), monkeypatch
        )
        result = await svc.query(SemanticQuery(metrics=["revenue"]), harness=True)
        assert result.result.rows == [[Decimal("100.0")]]

    async def test_ac2_normal_mode_does_not_block_on_mismatch(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _service(
            orders_source, [_assertion(4218334.10)], _revenue_result(4100000), monkeypatch
        )
        # A diverging assertion must NOT raise in normal mode — informational only.
        result = await svc.query(SemanticQuery(metrics=["revenue"]), harness=False)
        assert result.result.rows == [[4100000]]

    async def test_ac3_tolerance_within_one_percent_passes_harness(
        self, orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _service(
            orders_source,
            [_assertion(100.0, tolerance=0.01)],
            _revenue_result(99.5),
            monkeypatch,
        )
        result = await svc.query(SemanticQuery(metrics=["revenue"]), harness=True)
        assert result.result.rows == [[99.5]]
