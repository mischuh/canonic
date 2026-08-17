"""Knowledge search/read is scoped by a role's ``knowledge.allow_tags`` (SPEC-E12 §6, S15 AC2).

Distinct from — and layered on top of — the existing global/user-scope visibility rule
(SPEC-E6 §4, ``knowledge/scope.py``), which ``tests/knowledge/test_scope.py`` already covers.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import pytest

from canonic.config import CanonicConfig
from canonic.contracts.models import KnowledgePolicy, RoleDef, RolePolicy
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService

_DC_CONFIG = {
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

_PUBLIC_PAGE = """\
---
summary: "Public revenue definition."
tags: [public]
---

Revenue is the sum of paid order amounts.
"""

_INTERNAL_PAGE = """\
---
summary: "Internal margin methodology."
tags: [internal]
---

Margin methodology details, internal only.
"""


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "knowledge" / "global").mkdir(parents=True)
    (tmp_path / "knowledge" / "global" / "public.md").write_text(_PUBLIC_PAGE)
    (tmp_path / "knowledge" / "global" / "internal.md").write_text(_INTERNAL_PAGE)
    return tmp_path


@pytest.fixture
def role_policy() -> RolePolicy:
    return RolePolicy(
        schema_="roles/v1",
        claim="roles",
        roles={
            "merchant_viewer": RoleDef(knowledge=KnowledgePolicy(allow_tags=["public"])),
            "platform_analyst": RoleDef(knowledge=KnowledgePolicy(allow_tags=["*"])),
        },
    )


@pytest.fixture
def scoped_service(
    project_root: Path, role_policy: RolePolicy, monkeypatch: pytest.MonkeyPatch
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    resolver = ContractResolver(bindings=[], guardrails=[], roles=role_policy)
    config = CanonicConfig.model_validate(_DC_CONFIG)
    return CanonicService(config=config, resolver=resolver, sources=[], project_root=project_root)


_VIEWER = Principal(tenant=None, roles=("merchant_viewer",))
_ANALYST = Principal(tenant=None, roles=("platform_analyst",))


class TestSearchKnowledge:
    def test_viewer_sees_only_public_tagged_page(self, scoped_service: CanonicService) -> None:
        result = scoped_service.search_knowledge("revenue margin", principal=_VIEWER, limit=10)
        pages = {h.page for h in result.hits}
        assert pages == {"public"}

    def test_analyst_sees_every_tagged_page(self, scoped_service: CanonicService) -> None:
        result = scoped_service.search_knowledge("revenue margin", principal=_ANALYST, limit=10)
        pages = {h.page for h in result.hits}
        assert pages == {"public", "internal"}

    def test_no_role_policy_is_unrestricted(
        self, project_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absence is the feature switch: no role policy loaded at all behaves exactly like
        before this epic shipped, regardless of principal."""
        monkeypatch.setenv("PG_PASSWORD", "testpassword")
        resolver = ContractResolver(bindings=[], guardrails=[])
        config = CanonicConfig.model_validate(_DC_CONFIG)
        service = CanonicService(
            config=config, resolver=resolver, sources=[], project_root=project_root
        )
        result = service.search_knowledge("revenue margin", limit=10)
        pages = {h.page for h in result.hits}
        assert pages == {"public", "internal"}


class TestReadKnowledgePage:
    def test_viewer_can_read_public_page(self, scoped_service: CanonicService) -> None:
        page = scoped_service.read_knowledge_page("public", principal=_VIEWER)
        assert page["page_id"] == "public"

    def test_viewer_denied_internal_page(self, scoped_service: CanonicService) -> None:
        with pytest.raises(PermissionError):
            scoped_service.read_knowledge_page("internal", principal=_VIEWER)

    def test_analyst_can_read_internal_page(self, scoped_service: CanonicService) -> None:
        page = scoped_service.read_knowledge_page("internal", principal=_ANALYST)
        assert page["page_id"] == "internal"
