"""Unit tests for the knowledge-page models (GH-46)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from canonic.knowledge.models import (
    KnowledgePage,
    KnowledgePageMeta,
    KnowledgeScope,
    UsageMode,
)
from canonic.semantic.models import Provenance


def test_usage_mode_values() -> None:
    assert UsageMode.REFERENCE == "reference"
    assert UsageMode.CAVEAT == "caveat"
    assert UsageMode.POLICY == "policy"
    assert UsageMode.DEFINITION == "definition"


def test_scope_values() -> None:
    assert KnowledgeScope.GLOBAL == "global"
    assert KnowledgeScope.USER == "user"


def test_meta_defaults() -> None:
    meta = KnowledgePageMeta()
    assert meta.provenance is Provenance.INFERRED
    assert meta.last_validated_at is None
    assert meta.bound_fingerprints == {}
    assert meta.frozen is False


def test_page_defaults_for_optional_fields() -> None:
    """A page with only the derived fields set is valid (AC: optional fields omitted)."""
    page = KnowledgePage(
        id="customers-active",
        path=Path("knowledge/global/customers-active.md"),
        scope=KnowledgeScope.GLOBAL,
    )
    assert page.summary == ""
    assert page.tags == []
    assert page.sl_refs == []
    assert page.refs == []
    assert page.usage_mode is UsageMode.REFERENCE
    assert page.meta.frozen is False
    assert page.body == ""


def test_page_is_frozen() -> None:
    page = KnowledgePage(id="x", path=Path("knowledge/global/x.md"), scope=KnowledgeScope.GLOBAL)
    with pytest.raises(ValidationError):
        page.summary = "mutated"  # type: ignore[misc]
