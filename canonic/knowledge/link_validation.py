"""Checks that the links between metrics and knowledge pages name something that exists."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.contracts.loader import load_metric_bindings
from canonic.contracts.models import Status
from canonic.exc import ContractError
from canonic.knowledge.loader import load_knowledge_page, scope_from_path, slug_from_path
from canonic.knowledge.models import KnowledgeScope

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["global_page_ids", "validate_knowledge_links"]


def global_page_ids(project_root: Path) -> set[str]:
    """The ids of the global knowledge pages, read from the file names without parsing a page.

    A broken page still counts, so a caller that only needs to know whether an id exists is not
    stopped by an unrelated page it cannot load.
    """
    knowledge_root = project_root / "knowledge"
    if not knowledge_root.is_dir():
        return set()
    return {
        slug_from_path(path)
        for path in knowledge_root.rglob("*.md")
        if scope_from_path(path) is KnowledgeScope.GLOBAL
    }


def validate_knowledge_links(project_root: Path) -> None:
    """Raise :class:`~canonic.exc.ContractError` for a link that points at nothing.

    - A metric binding's ``knowledge_refs`` must name global knowledge pages. A personal page
      cannot explain a metric every caller sees.
    - A page's ``metrics`` must name active metrics by their canonical name. An alias is
      rejected with the canonical name it stands for, since that is the name the link is
      matched on.
    """
    knowledge_root = project_root / "knowledge"
    pages = (
        [load_knowledge_page(path) for path in sorted(knowledge_root.rglob("*.md"))]
        if knowledge_root.is_dir()
        else []
    )
    global_ids = {page.id for page in pages if page.scope is KnowledgeScope.GLOBAL}
    bindings = [b for b in load_metric_bindings(project_root) if b.status is Status.ACTIVE]
    canonical = {b.metric for b in bindings}
    canonical_by_alias = {alias: b.metric for b in bindings for alias in b.aliases}

    for binding in bindings:
        for ref in binding.knowledge_refs:
            if ref not in global_ids:
                raise ContractError(
                    f"metric {binding.metric!r}: knowledge_refs names {ref!r}, "
                    f"which is not a global knowledge page"
                )
    for page in pages:
        for name in page.metrics:
            if name in canonical:
                continue
            hint = (
                f", use its canonical name {canonical_by_alias[name]!r}"
                if name in canonical_by_alias
                else ""
            )
            raise ContractError(
                f"{page.path}: metrics names {name!r}, "
                f"which does not resolve to an active metric{hint}"
            )
