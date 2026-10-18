"""Knowledge capabilities: semantic search and live page rendering (E6, P1)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from canonic.config import LOCAL_STATE_DIR
from canonic.contracts.principal import Principal

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.compiler.result import CompileResult
    from canonic.core.context import ServiceContext
    from canonic.knowledge.embeddings import Embedder
    from canonic.knowledge.models import KnowledgePage
    from canonic.knowledge.results import SearchResult

_ANONYMOUS_PRINCIPAL = Principal()
_WILDCARD_TAG = "*"


class KnowledgeService:
    """Search and render knowledge pages, memoizing the embedding backend per instance."""

    def __init__(self, ctx: ServiceContext) -> None:
        self._ctx = ctx
        # Lazily constructed on first knowledge search and memoized: loading the embedding
        # backend can be slow, so it happens at most once per service instance, not once
        # per search call (SPEC-E10 §5, SPEC-E6 §5.2).
        self._embedder: Embedder | None = None
        self._embedder_checked = False
        self._pages: list[KnowledgePage] = []
        self._pages_signature: tuple[tuple[Path, int, int], ...] = ()

    def _tag_visible_pages(
        self, pages: list[KnowledgePage], principal: Principal | None
    ) -> list[KnowledgePage]:
        """Drop pages outside *principal*'s role ``knowledge.allow_tags`` (SPEC-E12 §6, S15 AC2).

        A page is visible when the allow set is the unrestricted wildcard (the default with
        no role policy loaded, or an explicit ``allow_tags: ["*"]``) or when at least one of
        its own tags is in the allow set. An untagged page under a restrictive allow set is
        excluded — an undeclared tag is a policy hole, same fail-closed posture as an
        undeclared tenancy source (§3 stage 2b), not an implicit pass.
        """
        allow_tags = self._ctx.resolver.authz_for(
            principal if principal is not None else _ANONYMOUS_PRINCIPAL
        ).allow_tags
        if _WILDCARD_TAG in allow_tags:
            return pages
        return [p for p in pages if any(t in allow_tags for t in p.tags)]

    def link_guardrails(
        self, compiled: CompileResult, principal: Principal | None
    ) -> CompileResult:
        """Fill ``knowledge_refs`` on the guardrails a compile fired.

        Only global pages the caller's role may see are considered: a compile carries no user,
        and a personal page must not be attached to a shared answer. Returns ``compiled``
        unchanged when no guardrail fired or the project has no knowledge directory.
        """
        from dataclasses import replace

        from canonic.knowledge import KnowledgeScope
        from canonic.knowledge.guardrail_refs import GuardrailKnowledgeLinker

        if not compiled.guardrails_fired:
            return compiled
        pages = [p for p in self._loaded_pages() if p.scope is KnowledgeScope.GLOBAL]
        if not pages:
            return compiled
        linker = GuardrailKnowledgeLinker(self._tag_visible_pages(pages, principal))
        return replace(compiled, guardrails_fired=linker.link(compiled.guardrails_fired))

    def _loaded_pages(self) -> list[KnowledgePage]:
        """All loaded pages, re-read only when a file under ``knowledge/`` changed.

        Compiling happens on every query, so the knowledge directory is not parsed each time:
        the pages are kept until the set of files, or the modification time or size of one, changes.
        """
        from canonic.knowledge import load_knowledge_page

        if self._ctx.project_root is None:
            return []
        knowledge_root = self._ctx.project_root / "knowledge"
        if not knowledge_root.exists():
            return []
        paths = sorted(knowledge_root.rglob("*.md"))
        signature = tuple((path, path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
        if signature != self._pages_signature:
            self._pages = [load_knowledge_page(path) for path in paths]
            self._pages_signature = signature
        return self._pages

    def search_knowledge(
        self,
        query: str,
        *,
        user: str | None = None,
        limit: int = 10,
        principal: Principal | None = None,
    ) -> SearchResult:
        """Search knowledge pages for business context (E6, P1).

        Returns ranked hits (definitions, policies) and any caveats auto-surfaced
        because a hit references their bound semantic entity. Returns an empty
        result when no project root or knowledge directory is available.

        ``principal``'s role ``knowledge.allow_tags`` filters the searchable corpus before
        ranking (SPEC-E12 §6, S15 AC2) — on top of, not instead of, the existing
        global/user-scope visibility ``user`` already governs (E6 §4).
        """
        from canonic.knowledge import EntityIndex, KnowledgeSearch, load_knowledge_page
        from canonic.knowledge.results import SearchResult as SR

        if self._ctx.project_root is None:
            return SR(hits=[], caveats=[])
        knowledge_root = self._ctx.project_root / "knowledge"
        if not knowledge_root.exists():
            return SR(hits=[], caveats=[])

        pages = [load_knowledge_page(p) for p in sorted(knowledge_root.rglob("*.md"))]
        pages = self._tag_visible_pages(pages, principal)
        if not pages:
            return SR(hits=[], caveats=[])

        # Live entity index so a returned page whose bound measure definition drifted is
        # flagged for prose review (§7).
        entity_index = EntityIndex.from_sources(self._ctx.sources)
        embedder = self._get_embedder()
        vectors = None
        if embedder is not None:
            from canonic.knowledge.vector_cache import VectorIndexCache

            cache_path = (
                self._ctx.project_root / LOCAL_STATE_DIR / "knowledge-index" / "vectors.json"
            )
            vectors = VectorIndexCache(cache_path).load_or_build(pages, embedder)
        return KnowledgeSearch(
            pages, embedder=embedder, vectors=vectors, entity_index=entity_index
        ).search(query, requesting_user=user or "anonymous", limit=limit)

    def _get_embedder(self) -> Embedder | None:
        """Return the memoized embedding runtime, or ``None`` when unavailable (§5.2).

        Constructed at most once per service instance — loading the local embedding
        backend can be slow, so repeated searches must not reload it (SPEC-E10 §5).
        """
        if not self._embedder_checked:
            from canonic.runtime.embeddings import EmbeddingRuntime

            runtime = EmbeddingRuntime(self._ctx.config.embeddings)
            self._embedder = runtime if runtime.is_available() else None
            self._embedder_checked = True
        return self._embedder

    def read_knowledge_page(
        self, page: str, *, user: str | None = None, principal: Principal | None = None
    ) -> dict[str, Any]:
        """Retrieve the full content of a knowledge page by page id with live rendering (E6, P1).

        Returns rendered body (with {{ sl:entity.expr }} directives resolved to live SQL),
        drift flag, and a staleness signal (``None`` while within the validation window). Respects access control.
        Per amendment-knowledge-read-page: body is rendered, meta includes last_validated_at and drift_flag.

        ``principal``'s role ``knowledge.allow_tags`` applies on top of the existing
        global/user-scope check (SPEC-E12 §6, S15 AC2): a page outside the allow set is
        refused with :class:`PermissionError`, same as an out-of-scope user page.
        """
        from canonic.knowledge import load_knowledge_page, user_from_path
        from canonic.knowledge.drift import DriftDetector
        from canonic.knowledge.rendering import DefinitionRenderer
        from canonic.knowledge.validation import EntityIndex

        if self._ctx.project_root is None:
            raise KeyError(f"No project root; cannot load knowledge page {page!r}")
        knowledge_root = self._ctx.project_root / "knowledge"
        if not knowledge_root.exists():
            raise KeyError(f"Knowledge directory not found; cannot load page {page!r}")

        pages = [load_knowledge_page(p) for p in sorted(knowledge_root.rglob("*.md"))]
        requesting_user = user or "anonymous"

        knowledge_page = None
        for p in pages:
            if p.id == page:
                page_owner = user_from_path(p.path)
                if p.scope.value == "global" or page_owner == requesting_user:
                    knowledge_page = p
                    break
                raise PermissionError(
                    f"User {requesting_user!r} does not have access to page {page!r}"
                )

        if knowledge_page is None:
            raise KeyError(f"Knowledge page {page!r} not found")
        if not self._tag_visible_pages([knowledge_page], principal):
            raise PermissionError(f"role does not permit access to page {page!r}")

        # Live entity index for rendering and drift detection (E6 §7).
        entity_index = EntityIndex.from_sources(self._ctx.sources)

        # Render body with live measure definitions ({{ sl:entity.expr }} → live SQL).
        renderer = DefinitionRenderer(entity_index)
        rendered_body = renderer.render(knowledge_page)

        # Detect drift: compare recorded bound_fingerprints with live definitions.
        detector = DriftDetector()
        drifted_refs = detector.flagged_for_review(knowledge_page, entity_index)
        has_drift = len(drifted_refs) > 0

        # Staleness: references not validated within the configured window (E6 §8, S8).
        window = timedelta(days=self._ctx.config.knowledge.staleness_window_days)
        signal = detector.staleness(knowledge_page, window=window)
        staleness = (
            {"age_days": signal.age_days, "message": signal.message} if signal is not None else None
        )

        return {
            "page_id": knowledge_page.id,
            "scope": knowledge_page.scope.value,
            "summary": knowledge_page.summary,
            "body": rendered_body,
            "tags": knowledge_page.tags,
            "sl_refs": knowledge_page.sl_refs,
            "refs": knowledge_page.refs,
            "usage_mode": knowledge_page.usage_mode.value,
            "meta": {
                "last_validated_at": (
                    knowledge_page.meta.last_validated_at.isoformat()
                    if knowledge_page.meta.last_validated_at
                    else None
                ),
                "drift_flag": has_drift,
                "staleness": staleness,
            },
        }
