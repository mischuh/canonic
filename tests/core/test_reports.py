"""Tests for ReportService / CanonicService.{list_reports,run_report,validate_reports}
(AMENDMENT-curated-reports).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
import pytest

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
from canonic.core.service import CanonicService
from canonic.exc import ReportError, ReportNotFound
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

if TYPE_CHECKING:
    from pathlib import Path

_SEED_SQL = """
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    amount   DECIMAL(12, 2),
    status   VARCHAR,
    segment  VARCHAR
);

INSERT INTO orders VALUES (1, 100.00, 'paid', 'business');
INSERT INTO orders VALUES (2, 50.00,  'paid', 'personal');
"""


@pytest.fixture
def duckdb_path(tmp_path: Path) -> Path:
    """A seeded DuckDB file with an `orders` table (executed by run_report's query() calls)."""
    db_path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(_SEED_SQL)
    con.close()
    return db_path


def _config(db_path: Path) -> dict:
    return {
        "version": 1,
        "project": {"name": "test", "default_connection": "warehouse_duckdb"},
        "connections": [
            {"id": "warehouse_duckdb", "type": "duckdb", "params": {"path": str(db_path)}}
        ],
        "llm": {
            "provider": "openai_compatible",
            "base_url": "http://localhost/v1",
            "model": "llama3",
        },
    }


@pytest.fixture
def orders_source() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="warehouse_duckdb",
        table="orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
            Column(name="segment", type="string", nullable=False),
        ],
        measures=[
            Measure(name="total_revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="order_count", expr="count(order_id)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="status", column="status"),
            Dimension(name="segment", column="segment"),
        ],
    )


@pytest.fixture
def revenue_binding() -> MetricBinding:
    return MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        status=Status.ACTIVE,
    )


@pytest.fixture
def order_count_binding() -> MetricBinding:
    return MetricBinding(
        metric="order_count",
        canonical=CanonicalRef(source="orders", measure="order_count"),
        status=Status.ACTIVE,
    )


@pytest.fixture
def required_segment_guardrail() -> Guardrail:
    """A required_dimension guardrail that GUARDRAIL_BLOCKs order_count without 'segment'."""
    return Guardrail(
        id="order-count-requires-segment",
        applies_to=AppliesTo(source="orders", measure="order_count"),
        kind=GuardrailKind.REQUIRED_DIMENSION,
        dimension="segment",
        severity=Severity.ERROR,
        rationale="order_count must always be broken down by segment.",
    )


def _write_knowledge_page(project_root: Path) -> None:
    knowledge_dir = project_root / "knowledge" / "global"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "revenue-definition.md").write_text(
        '---\nsummary: "What revenue means."\nusage_mode: definition\n---\n\n'
        "Revenue is the sum of order amounts.\n"
    )


def _write_report(project_root: Path, content: str, name: str = "customer_report.yaml") -> None:
    reports_dir = project_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / name).write_text(content)


@pytest.fixture
def report_service(
    tmp_path: Path,
    duckdb_path: Path,
    orders_source: SemanticSource,
    revenue_binding: MetricBinding,
    order_count_binding: MetricBinding,
    required_segment_guardrail: Guardrail,
) -> CanonicService:
    resolver = ContractResolver(
        bindings=[revenue_binding, order_count_binding], guardrails=[required_segment_guardrail]
    )
    config = CanonicConfig.model_validate(_config(duckdb_path))
    _write_knowledge_page(tmp_path)
    return CanonicService(
        config=config, resolver=resolver, sources=[orders_source], project_root=tmp_path
    )


class TestListReports:
    def test_empty_when_no_reports_dir(self, report_service: CanonicService) -> None:
        assert report_service.list_reports() == []

    def test_lists_committed_reports(self, report_service: CanonicService, tmp_path: Path) -> None:
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\ndescription: desc\nowner: data-team\n"
            "domain: orders\nsections:\n  - title: Revenue\n    query: {metrics: [revenue]}\n",
        )
        summaries = report_service.list_reports()
        assert len(summaries) == 1
        s = summaries[0]
        assert s.id == "customer_report"
        assert s.title == "Customer Report"
        assert s.description == "desc"
        assert s.owner == "data-team"
        assert s.domain == "orders"

    def test_domain_filter(self, report_service: CanonicService, tmp_path: Path) -> None:
        _write_report(
            tmp_path,
            "id: a\ntitle: A\ndomain: orders\nsections:\n"
            "  - title: s\n    query: {metrics: [revenue]}\n",
            name="a.yaml",
        )
        _write_report(
            tmp_path,
            "id: b\ntitle: B\ndomain: customers\nsections:\n"
            "  - title: s\n    query: {metrics: [revenue]}\n",
            name="b.yaml",
        )
        summaries = report_service.list_reports(domain="orders")
        assert [s.id for s in summaries] == ["a"]


class TestRunReport:
    async def test_two_sections_run_in_order_with_unmodified_query_result(
        self, report_service: CanonicService, tmp_path: Path
    ) -> None:
        """S16 AC1: sections execute in declared order with the full, unmodified QueryResult."""
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\nsections:\n"
            "  - title: Revenue by status\n"
            "    query: {metrics: [revenue], dimensions: [status]}\n"
            "  - title: Orders by segment\n"
            "    query: {metrics: [order_count], dimensions: [segment]}\n",
        )
        result = await report_service.run_report("customer_report")
        assert result.report_id == "customer_report"
        assert [s.title for s in result.sections] == ["Revenue by status", "Orders by segment"]
        assert result.sections[0].error is None
        assert result.sections[0].result is not None
        assert result.sections[0].result.compiled.sql  # unmodified QueryResult shape

    async def test_narrative_attached_and_rendered(
        self, report_service: CanonicService, tmp_path: Path
    ) -> None:
        """S16 AC2: narrative_from attaches the rendered body, never a raw template."""
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\nsections:\n"
            "  - title: Revenue by status\n"
            "    query: {metrics: [revenue], dimensions: [status]}\n"
            "    narrative_from: revenue-definition\n",
        )
        result = await report_service.run_report("customer_report")
        section = result.sections[0]
        assert section.narrative is not None
        assert section.narrative.page_id == "revenue-definition"
        assert "{{" not in section.narrative.body

    async def test_unknown_report_id_raises_report_not_found(
        self, report_service: CanonicService
    ) -> None:
        """S16 AC3: reuses the unresolved wire code — no new error code."""
        from canonic.exc import ErrorCode

        with pytest.raises(ReportNotFound) as exc:
            await report_service.run_report("does_not_exist")
        assert exc.value.code is ErrorCode.UNRESOLVED

    async def test_failing_section_does_not_abort_the_run(
        self, report_service: CanonicService, tmp_path: Path
    ) -> None:
        """S17 AC1: a GUARDRAIL_BLOCK on the middle section leaves the others intact."""
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\nsections:\n"
            "  - title: Revenue by status\n"
            "    query: {metrics: [revenue], dimensions: [status]}\n"
            "  - title: Orders (missing required segment)\n"
            "    query: {metrics: [order_count], dimensions: [status]}\n"
            "  - title: Orders by segment\n"
            "    query: {metrics: [order_count], dimensions: [segment]}\n",
        )
        result = await report_service.run_report("customer_report")

        first, second, third = result.sections
        assert first.error is None and first.result is not None
        assert second.error is not None and second.result is None
        assert second.error["code"] == "guardrail_block"
        assert third.error is None and third.result is not None

    async def test_context_merges_from_report_unless_section_overrides(
        self, report_service: CanonicService, tmp_path: Path
    ) -> None:
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\ncontext: eu\nsections:\n"
            "  - title: Revenue\n    query: {metrics: [revenue], dimensions: [status]}\n",
        )
        # No context-scoped guardrail is registered, so this only exercises that the
        # merged query still compiles and executes without error.
        result = await report_service.run_report("customer_report")
        assert result.sections[0].error is None


class TestValidateReports:
    def test_passes_for_valid_report(self, report_service: CanonicService, tmp_path: Path) -> None:
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\nsections:\n"
            "  - title: Revenue\n    query: {metrics: [revenue], dimensions: [status]}\n"
            "    narrative_from: revenue-definition\n",
        )
        report_service.validate_reports()  # must not raise

    def test_unresolvable_metric_fails_with_report_id_and_section_index(
        self, report_service: CanonicService, tmp_path: Path
    ) -> None:
        """S18 AC1."""
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\nsections:\n"
            "  - title: Revenue\n    query: {metrics: [revenue], dimensions: [status]}\n"
            "  - title: Bogus\n    query: {metrics: [does_not_resolve]}\n",
        )
        with pytest.raises(ReportError) as exc:
            report_service.validate_reports()
        assert "customer_report" in str(exc.value)
        assert "section 1" in str(exc.value)

    def test_dangling_narrative_from_fails(
        self, report_service: CanonicService, tmp_path: Path
    ) -> None:
        """S18 AC2."""
        _write_report(
            tmp_path,
            "id: customer_report\ntitle: Customer Report\nsections:\n"
            "  - title: Revenue\n    query: {metrics: [revenue]}\n"
            "    narrative_from: does-not-exist\n",
        )
        with pytest.raises(ReportError, match="does-not-exist"):
            report_service.validate_reports()
