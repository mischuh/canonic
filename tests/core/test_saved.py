"""Tests for SavedContentService / CanonicService.{save_query,list_saved_queries,delete_query,
compose_report,update_report,delete_report} (AMENDMENT-user-scoped-queries-reports, S19-S23, S25).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import duckdb
import pytest

from canonic.compiler.query import SemanticQuery
from canonic.config import CanonicConfig
from canonic.contracts.models import CanonicalRef, MetricBinding, RoleDef, RolePolicy, Status
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.exc import CanonicError, ReportError, ReportNotFound, TenantForbidden
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
def service(
    tmp_path: Path, duckdb_path: Path, orders_source: SemanticSource, revenue_binding: MetricBinding
) -> CanonicService:
    """A CanonicService over a real tmp project root (not a git repo — filesystem-only writes)."""
    resolver = ContractResolver(bindings=[revenue_binding], guardrails=[])
    config = CanonicConfig.model_validate(_config(duckdb_path))
    return CanonicService(
        config=config, resolver=resolver, sources=[orders_source], project_root=tmp_path
    )


@pytest.fixture
def git_service(
    tmp_path: Path, duckdb_path: Path, orders_source: SemanticSource, revenue_binding: MetricBinding
) -> CanonicService:
    """A CanonicService over a tmp project root that *is* a git work tree (S21 AC2)."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    resolver = ContractResolver(bindings=[revenue_binding], guardrails=[])
    config = CanonicConfig.model_validate(_config(duckdb_path))
    return CanonicService(
        config=config, resolver=resolver, sources=[orders_source], project_root=tmp_path
    )


@pytest.fixture
def roled_service(
    tmp_path: Path, duckdb_path: Path, orders_source: SemanticSource, revenue_binding: MetricBinding
) -> CanonicService:
    """A CanonicService with a role policy: 'viewer' denies, 'editor' grants manage_saved_content."""
    roles = RolePolicy(
        schema="roles/v1",
        claim="roles",
        roles={
            "viewer": RoleDef(metrics={"allow": ["*"]}, manage_saved_content=False),
            "editor": RoleDef(metrics={"allow": ["*"]}, manage_saved_content=True),
        },
    )
    resolver = ContractResolver(bindings=[revenue_binding], guardrails=[], roles=roles)
    config = CanonicConfig.model_validate(_config(duckdb_path))
    return CanonicService(
        config=config, resolver=resolver, sources=[orders_source], project_root=tmp_path
    )


def _query(metrics: list[str] | None = None, **kw: object) -> SemanticQuery:
    return SemanticQuery(metrics=metrics or ["revenue"], **kw)  # type: ignore[arg-type]


class TestSaveQuery:
    async def test_save_writes_a_retrievable_query(self, service: CanonicService) -> None:
        """S19 AC1/AC2: saved, then re-run for the same principal returns the query's result."""
        summary = await service.save_query(
            _query(dimensions=["status"]), title="Revenue by status", user="alice"
        )
        assert summary.metrics == ["revenue"]
        assert summary.dimensions == ["status"]

        listed = service.list_saved_queries(user="alice")
        assert [s.id for s in listed] == [summary.id]

        run = await service.run_report(summary.id, user="alice")
        assert run.sections[0].error is None
        assert {tuple(r) for r in run.sections[0].result.result.rows} == {("paid", 150.00)}

    async def test_save_rejects_query_that_does_not_compile(self, service: CanonicService) -> None:
        with pytest.raises(CanonicError):
            await service.save_query(_query(metrics=["nope"]), user="alice")

    async def test_save_without_user_is_refused(self, service: CanonicService) -> None:
        """Fail closed: never write under an implicit anonymous scope."""
        with pytest.raises(ReportError, match="no caller identity"):
            await service.save_query(_query(), user=None)

    async def test_different_principal_gets_not_found(self, service: CanonicService) -> None:
        """S19 AC3."""
        summary = await service.save_query(_query(), user="alice")
        with pytest.raises(ReportNotFound):
            await service.run_report(summary.id, user="bob")


class TestListSavedQueries:
    async def test_never_sees_another_users_queries(self, service: CanonicService) -> None:
        """S20 AC1."""
        await service.save_query(_query(), title="Alice's", user="alice")
        await service.save_query(_query(), title="Bob's", user="bob")
        assert [s.title for s in service.list_saved_queries(user="alice")] == ["Alice's"]
        assert [s.title for s in service.list_saved_queries(user="bob")] == ["Bob's"]

    async def test_never_includes_composed_reports(self, service: CanonicService) -> None:
        """S20 AC1: never alice's own report root either, only queries/."""
        q = await service.save_query(_query(), title="Q", user="alice")
        await service.compose_report("R", [q.id], user="alice")
        assert [s.id for s in service.list_saved_queries(user="alice")] == [q.id]

    def test_list_reports_never_includes_queries(self, service: CanonicService) -> None:
        """S20 AC2."""
        assert service.list_reports(user="alice") == []


class TestDeleteQuery:
    async def test_deletes_own_query(self, service: CanonicService) -> None:
        q = await service.save_query(_query(), user="alice")
        await service.delete_query(q.id, user="alice")
        assert service.list_saved_queries(user="alice") == []

    async def test_refused_for_another_users_query(self, service: CanonicService) -> None:
        """S21 AC3."""
        q = await service.save_query(_query(), user="alice")
        with pytest.raises(ReportNotFound):
            await service.delete_query(q.id, user="bob")
        assert [s.id for s in service.list_saved_queries(user="alice")] == [q.id]

    async def test_refused_for_a_report_id(self, service: CanonicService) -> None:
        """S23 AC2: the two id namespaces never resolve into each other."""
        q = await service.save_query(_query(), user="alice")
        r = await service.compose_report("R", [q.id], user="alice")
        with pytest.raises(ReportNotFound):
            await service.delete_query(r.id, user="alice")


class TestComposeReport:
    async def test_copies_definitions_not_references(self, service: CanonicService) -> None:
        """S22 AC1/AC2: editing/deleting the source afterwards doesn't change the report."""
        q1 = await service.save_query(_query(dimensions=["status"]), title="Q1", user="alice")
        q2 = await service.save_query(_query(dimensions=["segment"]), title="Q2", user="alice")
        report = await service.compose_report("Combined", [q1.id, q2.id], user="alice")

        before = await service.run_report(report.id, user="alice")
        await service.delete_query(q1.id, user="alice")
        await service.delete_query(q2.id, user="alice")
        after = await service.run_report(report.id, user="alice")

        assert len(before.sections) == len(after.sections) == 2
        assert [s.result.result.rows for s in before.sections] == [
            s.result.result.rows for s in after.sections
        ]

    async def test_refused_for_another_users_query(self, service: CanonicService) -> None:
        q = await service.save_query(_query(), user="alice")
        with pytest.raises(ReportNotFound):
            await service.compose_report("R", [q.id], user="bob")


class TestUpdateReport:
    async def test_add_from_appends_a_frozen_section(self, service: CanonicService) -> None:
        """S25 AC1/AC2/AC5."""
        q1 = await service.save_query(_query(dimensions=["status"]), title="Q1", user="alice")
        report = await service.compose_report("R", [q1.id], user="alice")
        q2 = await service.save_query(_query(dimensions=["segment"]), title="Q2", user="alice")

        updated = await service.update_report(report.id, add_from=[q2.id], user="alice")
        assert updated.id == report.id  # id unchanged

        run = await service.run_report(report.id, user="alice")
        assert len(run.sections) == 2

        await service.delete_query(q2.id, user="alice")
        run_after_delete = await service.run_report(report.id, user="alice")
        assert len(run_after_delete.sections) == 2  # frozen copy, unaffected

    async def test_remove_sections_drops_by_id(self, service: CanonicService) -> None:
        """S25 AC3."""
        q1 = await service.save_query(_query(dimensions=["status"]), title="Q1", user="alice")
        q2 = await service.save_query(_query(dimensions=["segment"]), title="Q2", user="alice")
        report = await service.compose_report("R", [q1.id, q2.id], user="alice")

        await service.update_report(report.id, remove_sections=[q1.id], user="alice")
        run = await service.run_report(report.id, user="alice")
        assert len(run.sections) == 1
        assert run.sections[0].title == "Q2"

    async def test_refused_for_another_users_report(self, service: CanonicService) -> None:
        """S25 AC4."""
        q = await service.save_query(_query(), user="alice")
        report = await service.compose_report("R", [q.id], user="alice")
        with pytest.raises(ReportNotFound):
            await service.update_report(report.id, remove_sections=[], user="bob")

    async def test_removing_every_section_is_rejected(self, service: CanonicService) -> None:
        q = await service.save_query(_query(), user="alice")
        report = await service.compose_report("R", [q.id], user="alice")
        with pytest.raises(ReportError):
            await service.update_report(report.id, remove_sections=[q.id], user="alice")


class TestDeleteReport:
    async def test_delete_report_never_deletes_source_queries(
        self, service: CanonicService
    ) -> None:
        """S23 AC1."""
        q1 = await service.save_query(_query(), title="Q1", user="alice")
        q2 = await service.save_query(_query(), title="Q2", user="alice")
        report = await service.compose_report("R", [q1.id, q2.id], user="alice")

        await service.delete_report(report.id, user="alice")

        remaining = {s.id for s in service.list_saved_queries(user="alice")}
        assert remaining == {q1.id, q2.id}
        with pytest.raises(ReportNotFound):
            await service.run_report(report.id, user="alice")

    async def test_refused_for_a_query_id(self, service: CanonicService) -> None:
        """S23 AC2."""
        q = await service.save_query(_query(), user="alice")
        with pytest.raises(ReportNotFound):
            await service.delete_report(q.id, user="alice")

    async def test_refused_for_another_users_report(self, service: CanonicService) -> None:
        q = await service.save_query(_query(), user="alice")
        report = await service.compose_report("R", [q.id], user="alice")
        with pytest.raises(ReportNotFound):
            await service.delete_report(report.id, user="bob")


class TestGetOverviewReports:
    """S24: get_overview surfaces both report scopes and saved-query questions."""

    async def test_reports_field_lists_both_scopes(self, service: CanonicService) -> None:
        """S24 AC2."""
        q = await service.save_query(_query(), user="alice")
        report = await service.compose_report("Alice's report", [q.id], user="alice")

        overview = service.get_overview(user="alice")
        refs = {r.id: r.scope for r in overview.reports}
        assert refs[report.id] == "user:alice"

    async def test_never_alices_own_reports_for_bob(self, service: CanonicService) -> None:
        q = await service.save_query(_query(), user="alice")
        report = await service.compose_report("Alice's report", [q.id], user="alice")

        overview = service.get_overview(user="bob")
        assert report.id not in {r.id for r in overview.reports}

    async def test_saved_query_question_feeds_sample_questions_not_reports(
        self, service: CanonicService
    ) -> None:
        """S24 AC3: the question surfaces in sample_questions, but the query itself is never
        listed under ``reports``."""
        q = await service.save_query(
            _query(dimensions=["status"]),
            question="What was revenue by status, exactly?",
            user="alice",
        )

        overview = service.get_overview(user="alice")
        assert q.id not in {r.id for r in overview.reports}
        orders_group = next(g for g in overview.domains if g.name == "orders")
        assert "What was revenue by status, exactly?" in orders_group.sample_questions

    async def test_no_user_omits_saved_query_questions(self, service: CanonicService) -> None:
        await service.save_query(
            _query(dimensions=["status"]), question="Only alice's question", user="alice"
        )
        overview = service.get_overview()
        for group in overview.domains:
            assert "Only alice's question" not in group.sample_questions


class TestGitCommitOnWrite:
    """S21 AC1/AC2: personal writes bypass PR review but stay auditable as git commits."""

    async def test_save_query_produces_an_attributed_commit(
        self, git_service: CanonicService, tmp_path: Path
    ) -> None:
        await git_service.save_query(_query(), user="alice")

        log = subprocess.run(
            ["git", "log", "--format=%an"], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        assert log.stdout.strip() == "alice"

    async def test_no_merge_or_approval_step_required(
        self, git_service: CanonicService, tmp_path: Path
    ) -> None:
        """S21 AC1: completes in a single call, no separate approval step."""
        q = await git_service.save_query(_query(), user="alice")
        report = await git_service.compose_report("R", [q.id], user="alice")
        await git_service.delete_report(report.id, user="alice")
        await git_service.delete_query(q.id, user="alice")

        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        assert (
            len(log.stdout.strip().splitlines()) == 4
        )  # save, compose, delete-report, delete-query


class TestManageSavedContentGate:
    """A role with manage_saved_content: false is refused all six capabilities outright."""

    async def test_save_query_denied_for_viewer(self, roled_service: CanonicService) -> None:
        principal = Principal(tenant=None, roles=("viewer",))
        with pytest.raises(TenantForbidden, match="manage_saved_content"):
            await roled_service.save_query(_query(), user="alice", principal=principal)

    def test_list_saved_queries_denied_for_viewer(self, roled_service: CanonicService) -> None:
        principal = Principal(tenant=None, roles=("viewer",))
        with pytest.raises(TenantForbidden, match="manage_saved_content"):
            roled_service.list_saved_queries(user="alice", principal=principal)

    async def test_delete_query_denied_for_viewer(self, roled_service: CanonicService) -> None:
        editor = Principal(tenant=None, roles=("editor",))
        q = await roled_service.save_query(_query(), user="alice", principal=editor)

        viewer = Principal(tenant=None, roles=("viewer",))
        with pytest.raises(TenantForbidden, match="manage_saved_content"):
            await roled_service.delete_query(q.id, user="alice", principal=viewer)

    async def test_compose_report_denied_for_viewer(self, roled_service: CanonicService) -> None:
        editor = Principal(tenant=None, roles=("editor",))
        q = await roled_service.save_query(_query(), user="alice", principal=editor)

        viewer = Principal(tenant=None, roles=("viewer",))
        with pytest.raises(TenantForbidden, match="manage_saved_content"):
            await roled_service.compose_report("R", [q.id], user="alice", principal=viewer)

    async def test_update_report_denied_for_viewer(self, roled_service: CanonicService) -> None:
        editor = Principal(tenant=None, roles=("editor",))
        q = await roled_service.save_query(_query(), user="alice", principal=editor)
        report = await roled_service.compose_report("R", [q.id], user="alice", principal=editor)

        viewer = Principal(tenant=None, roles=("viewer",))
        with pytest.raises(TenantForbidden, match="manage_saved_content"):
            await roled_service.update_report(report.id, user="alice", principal=viewer)

    async def test_delete_report_denied_for_viewer(self, roled_service: CanonicService) -> None:
        editor = Principal(tenant=None, roles=("editor",))
        q = await roled_service.save_query(_query(), user="alice", principal=editor)
        report = await roled_service.compose_report("R", [q.id], user="alice", principal=editor)

        viewer = Principal(tenant=None, roles=("viewer",))
        with pytest.raises(TenantForbidden, match="manage_saved_content"):
            await roled_service.delete_report(report.id, user="alice", principal=viewer)

    async def test_editor_role_may_manage_own_content(self, roled_service: CanonicService) -> None:
        """The gate is fail-closed, not fail-everything: a granting role still works end to end."""
        editor = Principal(tenant=None, roles=("editor",))
        q = await roled_service.save_query(_query(), user="alice", principal=editor)
        listed = roled_service.list_saved_queries(user="alice", principal=editor)
        assert [s.id for s in listed] == [q.id]
        report = await roled_service.compose_report("R", [q.id], user="alice", principal=editor)
        await roled_service.update_report(report.id, user="alice", principal=editor)
        await roled_service.delete_report(report.id, user="alice", principal=editor)
        await roled_service.delete_query(q.id, user="alice", principal=editor)

    async def test_unauthenticated_caller_denied_when_role_policy_loaded(
        self, roled_service: CanonicService
    ) -> None:
        """No principal at all resolves to the anonymous principal, not an unrestricted one."""
        with pytest.raises(TenantForbidden, match="manage_saved_content"):
            await roled_service.save_query(_query(), user="alice")
