"""Knowledge page layer: typed models and Markdown-frontmatter IO for knowledge/**/*.md."""

from __future__ import annotations

from canon.knowledge.loader import (
    load_knowledge_page,
    scope_from_path,
    slug_from_path,
    user_from_path,
)
from canon.knowledge.models import (
    KnowledgePage,
    KnowledgePageMeta,
    KnowledgeScope,
    KnowledgeValidationError,
    UsageMode,
)
from canon.knowledge.scope import (
    CollisionResult,
    ScopeResolver,
)
from canon.knowledge.validation import (
    EntityIndex,
    PageIndex,
    ReferenceValidator,
)

__all__ = [
    "CollisionResult",
    "EntityIndex",
    "KnowledgePage",
    "KnowledgePageMeta",
    "KnowledgeScope",
    "KnowledgeValidationError",
    "PageIndex",
    "ReferenceValidator",
    "ScopeResolver",
    "UsageMode",
    "load_knowledge_page",
    "scope_from_path",
    "slug_from_path",
    "user_from_path",
]
