"""Knowledge page layer: typed models and Markdown-frontmatter IO for knowledge/**/*.md."""

from __future__ import annotations

from canon.knowledge.loader import (
    load_knowledge_page,
    scope_from_path,
    slug_from_path,
)
from canon.knowledge.models import (
    KnowledgePage,
    KnowledgePageMeta,
    KnowledgeScope,
    KnowledgeValidationError,
    UsageMode,
)
from canon.knowledge.validation import (
    EntityIndex,
    PageIndex,
    ReferenceValidator,
)

__all__ = [
    "EntityIndex",
    "KnowledgePage",
    "KnowledgePageMeta",
    "KnowledgeScope",
    "KnowledgeValidationError",
    "PageIndex",
    "ReferenceValidator",
    "UsageMode",
    "load_knowledge_page",
    "scope_from_path",
    "slug_from_path",
]
