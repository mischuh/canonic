"""Acceptance-criteria tests for the knowledge-page loader (GH-46)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from canon.exc import KnowledgePageError
from canon.knowledge.loader import (
    load_knowledge_page,
    scope_from_path,
    slug_from_path,
)
from canon.knowledge.models import KnowledgeScope, UsageMode
from canon.semantic.models import Provenance


def test_load_full_page_round_trips_fields(write_page, valid_page_md: str) -> None:
    path = write_page(valid_page_md)
    page = load_knowledge_page(path)

    assert page.id == "customers-active"
    assert page.path == path
    assert page.scope is KnowledgeScope.GLOBAL
    assert page.summary.startswith("Why test accounts")
    assert page.tags == ["customers", "definitions"]
    assert page.sl_refs == [
        "warehouse_pg.customers",
        "warehouse_pg.orders.total_revenue",
    ]
    assert page.refs == ["test-account-policy"]
    assert page.usage_mode is UsageMode.CAVEAT
    assert page.meta.provenance is Provenance.HUMAN_CURATED
    assert page.meta.last_validated_at == datetime(2026, 6, 14, tzinfo=UTC)
    assert page.meta.bound_fingerprints == {"warehouse_pg.orders.total_revenue": "sha256:abc"}
    assert page.meta.frozen is False
    # Body is preserved verbatim, including the unresolved directives.
    assert "[[test-account-policy]]" in page.body
    assert "{{ sl:warehouse_pg.orders.total_revenue.expr }}" in page.body


def test_scope_derivation_global(write_page) -> None:
    path = write_page("---\nsummary: g\n---\nbody\n", rel_path="global/g.md")
    assert load_knowledge_page(path).scope is KnowledgeScope.GLOBAL


def test_scope_derivation_user(write_page) -> None:
    path = write_page("---\nsummary: u\n---\nbody\n", rel_path="user/alice/u.md")
    assert load_knowledge_page(path).scope is KnowledgeScope.USER


def test_scope_from_path_pure() -> None:
    from pathlib import Path

    assert scope_from_path(Path("knowledge/global/x.md")) is KnowledgeScope.GLOBAL
    assert scope_from_path(Path("knowledge/user/bob/y.md")) is KnowledgeScope.USER


def test_scope_unknown_segment_raises() -> None:
    from pathlib import Path

    with pytest.raises(KnowledgePageError, match="unknown scope segment"):
        scope_from_path(Path("knowledge/team/x.md"))


def test_scope_no_knowledge_dir_raises() -> None:
    from pathlib import Path

    with pytest.raises(KnowledgePageError, match="not under a 'knowledge/'"):
        scope_from_path(Path("docs/global/x.md"))


def test_slug_derivation() -> None:
    from pathlib import Path

    assert slug_from_path(Path("knowledge/global/customers-active.md")) == "customers-active"


def test_all_optional_fields_omitted_is_valid(write_page) -> None:
    """A page with empty frontmatter loads with sensible defaults (AC)."""
    path = write_page("---\n---\nJust a body.\n")
    page = load_knowledge_page(path)
    assert page.summary == ""
    assert page.tags == []
    assert page.usage_mode is UsageMode.REFERENCE
    assert page.meta.frozen is False
    assert page.body == "Just a body.\n"


def test_no_frontmatter_fence_is_all_body(write_page) -> None:
    path = write_page("No frontmatter at all.\n")
    page = load_knowledge_page(path)
    assert page.body == "No frontmatter at all.\n"
    assert page.usage_mode is UsageMode.REFERENCE


def test_frozen_default_false(write_page) -> None:
    path = write_page("---\nsummary: s\n---\nbody\n")
    assert load_knowledge_page(path).meta.frozen is False


def test_frozen_true_loads_cleanly(write_page) -> None:
    """A page with frozen: true in frontmatter loads cleanly (AC)."""
    path = write_page("---\nmeta:\n  frozen: true\n---\nbody\n")
    assert load_knowledge_page(path).meta.frozen is True


def test_hand_set_scope_is_rejected(write_page) -> None:
    """scope is derived from path, never accepted as a hand-set field (AC)."""
    path = write_page("---\nscope: global\nsummary: s\n---\nbody\n")
    with pytest.raises(KnowledgePageError, match="'scope' is derived from the path"):
        load_knowledge_page(path)


def test_hand_set_id_is_rejected(write_page) -> None:
    path = write_page("---\nid: custom\n---\nbody\n")
    with pytest.raises(KnowledgePageError, match="'id' is derived from the path"):
        load_knowledge_page(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(KnowledgePageError, match="not found"):
        load_knowledge_page(tmp_path / "knowledge" / "global" / "nope.md")


def test_invalid_usage_mode_reports_file(write_page) -> None:
    path = write_page("---\nusage_mode: nonsense\n---\nbody\n")
    with pytest.raises(KnowledgePageError) as exc:
        load_knowledge_page(path)
    assert str(path) in str(exc.value)
