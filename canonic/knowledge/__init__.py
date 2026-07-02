"""Knowledge page layer: typed models and Markdown-frontmatter IO for knowledge/**/*.md."""

from __future__ import annotations

from canonic.knowledge.drift import DriftDetector, StalenessSignal
from canonic.knowledge.embeddings import Embedder, VectorStore
from canonic.knowledge.index import KnowledgeIndex
from canonic.knowledge.loader import (
    load_knowledge_page,
    scope_from_path,
    slug_from_path,
    user_from_path,
)
from canonic.knowledge.models import (
    KnowledgePage,
    KnowledgePageMeta,
    KnowledgeScope,
    KnowledgeValidationError,
    UsageMode,
)
from canonic.knowledge.rendering import DefinitionRenderer
from canonic.knowledge.results import (
    Annotation,
    Caveat,
    Hit,
    MatchedOn,
    ReviewFlag,
    SearchResult,
    Subgraph,
)
from canonic.knowledge.retrieval import KnowledgeSearch
from canonic.knowledge.scope import (
    CollisionResult,
    ScopeResolver,
)
from canonic.knowledge.traversal import GraphTraversal
from canonic.knowledge.validation import (
    EntityIndex,
    PageIndex,
    ReferenceValidator,
)

__all__ = [
    "Annotation",
    "Caveat",
    "CollisionResult",
    "DefinitionRenderer",
    "DriftDetector",
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
    "ReviewFlag",
    "ScopeResolver",
    "SearchResult",
    "StalenessSignal",
    "Subgraph",
    "UsageMode",
    "VectorStore",
    "load_knowledge_page",
    "scope_from_path",
    "slug_from_path",
    "user_from_path",
]
