"""Knowledge page layer: typed models and Markdown-frontmatter IO for knowledge/**/*.md."""

from __future__ import annotations

from canon.knowledge.embeddings import Embedder, VectorStore
from canon.knowledge.index import KnowledgeIndex
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
from canon.knowledge.results import (
    Annotation,
    Hit,
    MatchedOn,
    SearchResult,
    Subgraph,
)
from canon.knowledge.retrieval import KnowledgeSearch
from canon.knowledge.scope import (
    CollisionResult,
    ScopeResolver,
)
from canon.knowledge.traversal import GraphTraversal
from canon.knowledge.validation import (
    EntityIndex,
    PageIndex,
    ReferenceValidator,
)

__all__ = [
    "Annotation",
    "CollisionResult",
    "Embedder",
    "EntityIndex",
    "GraphTraversal",
    "Hit",
    "KnowledgeIndex",
    "KnowledgePage",
    "KnowledgePageMeta",
    "KnowledgeScope",
    "KnowledgeSearch",
    "KnowledgeValidationError",
    "MatchedOn",
    "PageIndex",
    "ReferenceValidator",
    "ScopeResolver",
    "SearchResult",
    "Subgraph",
    "UsageMode",
    "VectorStore",
    "load_knowledge_page",
    "scope_from_path",
    "slug_from_path",
    "user_from_path",
]
