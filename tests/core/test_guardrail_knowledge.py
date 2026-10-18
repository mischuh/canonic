"""A served query links its fired guardrails to the knowledge pages that explain them."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import pytest

from canonic.compiler import SemanticQuery
from canonic.config import CanonicConfig
from canonic.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    KnowledgePolicy,
    MetricBinding,
    RoleDef,
    RolePolicy,
    Severity,
)
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import CompileOutput
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
        Column(name="status", type="string", nullable=False),
    ],
    measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
    dimensions=[Dimension(name="status", column="status")],
)


def _page(tags: str, sl_ref: str) -> str:
    return f'---\nsummary: "s"\ntags: [{tags}]\nsl_refs: ["{sl_ref}"]\n---\n\nBody.\n'


@pytest.fixture
def project(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    (knowledge / "global").mkdir(parents=True)
    (knowledge / "user" / "alice").mkdir(parents=True)
    status = "warehouse_pg.orders.status"
    (knowledge / "global" / "refund-policy.md").write_text(_page("public", status))
    (knowledge / "global" / "refund-internals.md").write_text(_page("internal", status))
    (knowledge / "global" / "unrelated.md").write_text(_page("public", "warehouse_pg.orders.id"))
    (knowledge / "user" / "alice" / "my-notes.md").write_text(_page("public", status))
    return tmp_path


def _service(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    knowledge_refs: list[str] | None = None,
    roles: RolePolicy | None = None,
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    guardrail = Guardrail(
        id="revenue-excludes-refunds",
        applies_to=AppliesTo(source="orders", measure="total_revenue"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="status != 'refunded'",
        severity=Severity.ERROR,
        rationale="Refunds are reversals.",
        knowledge_refs=knowledge_refs or [],
    )
    binding = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="total_revenue")
    )
    return CanonicService(
        config=CanonicConfig.model_validate(_CONFIG),
        resolver=ContractResolver(bindings=[binding], guardrails=[guardrail], roles=roles),
        sources=[_ORDERS],
        project_root=project,
    )


def _fired(service: CanonicService, principal: Principal | None = None):
    compiled = service.compile_query(SemanticQuery(metrics=["revenue"]), principal=principal)
    (fired,) = CompileOutput.from_compile_result(compiled).metadata.guardrails_fired
    return fired


def test_fired_guardrail_lists_the_global_pages_that_reference_its_entities(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fired = _fired(_service(project, monkeypatch))

    assert fired.predicate == "status != 'refunded'"
    assert fired.knowledge_refs == ["refund-internals", "refund-policy"]


def test_user_pages_are_never_attached_to_a_compile(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "my-notes" not in _fired(_service(project, monkeypatch)).knowledge_refs


def test_declared_refs_replace_the_inferred_pages(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(project, monkeypatch, knowledge_refs=["unrelated"])

    assert _fired(service).knowledge_refs == ["unrelated"]


def test_pages_outside_the_role_tags_are_not_listed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles = RolePolicy(
        schema_="roles/v1",
        claim="roles",
        roles={"viewer": RoleDef(knowledge=KnowledgePolicy(allow_tags=["public"]))},
    )
    service = _service(project, monkeypatch, roles=roles)

    assert _fired(service, Principal(roles=("viewer",))).knowledge_refs == ["refund-policy"]


def test_a_project_without_knowledge_has_empty_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _fired(_service(tmp_path, monkeypatch)).knowledge_refs == []


def test_pages_are_reread_after_a_file_changes(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(project, monkeypatch)
    assert "refund-policy" in _fired(service).knowledge_refs

    (project / "knowledge" / "global" / "refund-policy.md").write_text(
        _page("public", "warehouse_pg.orders.id")
    )

    assert "refund-policy" not in _fired(service).knowledge_refs
