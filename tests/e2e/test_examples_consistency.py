"""Every shipped example project stays consistent with the current schemas.

``tests/golden`` locks the numbers of the zero-infrastructure examples, and the e2e tests
cover ``marketplace`` and ``ossie-retail``. None of them load an example's knowledge pages,
and ``ecommerce`` needs a live Postgres, so a page whose ``sl_refs`` stopped resolving, or an
example that fell behind a contract change, went unnoticed. This loads each project with the
real loaders and needs neither a database nor a network.

Staleness is deliberately not asserted: it depends on today's date, so the test would start
failing on its own once a page passes the staleness window.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from canonic.contracts.validate import validate_contracts
from canonic.knowledge.drift import DriftDetector
from canonic.knowledge.loader import load_knowledge_page
from canonic.knowledge.rendering import DefinitionRenderer
from canonic.knowledge.validation import EntityIndex, PageIndex, ReferenceValidator
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from canonic.knowledge.models import KnowledgePage

_EXAMPLES = Path(__file__).parents[2] / "examples"
_PROJECTS = sorted(p.name for p in _EXAMPLES.iterdir() if (p / "canonic.yaml").is_file())


def _pages(project: Path) -> list[KnowledgePage]:
    return [load_knowledge_page(path) for path in sorted((project / "knowledge").rglob("*.md"))]


def test_every_example_project_is_discovered() -> None:
    assert len(_PROJECTS) >= 8


@pytest.mark.parametrize("name", _PROJECTS)
def test_contracts_validate_against_the_semantic_sources(name: str) -> None:
    validate_contracts(_EXAMPLES / name)


@pytest.mark.parametrize("name", _PROJECTS)
def test_knowledge_references_resolve(name: str) -> None:
    project = _EXAMPLES / name
    pages = _pages(project)
    entities = EntityIndex.from_sources(list_semantic_sources(project))
    validator = ReferenceValidator(entities, PageIndex.from_pages(pages))
    for page in pages:
        validator.validate_page(page)


@pytest.mark.parametrize("name", _PROJECTS)
def test_knowledge_pages_render_and_carry_no_drifted_fingerprint(name: str) -> None:
    project = _EXAMPLES / name
    entities = EntityIndex.from_sources(list_semantic_sources(project))
    renderer = DefinitionRenderer(entities)
    detector = DriftDetector()
    for page in _pages(project):
        assert "{{ sl:" not in renderer.render(page), f"{page.path}: unresolved sl directive"
        assert detector.flagged_for_review(page, entities) == [], f"{page.path}: drifted"
