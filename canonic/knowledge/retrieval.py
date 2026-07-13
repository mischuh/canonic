"""Hybrid retrieval engine over committed knowledge pages (SPEC-E6 §5, §10).

:class:`KnowledgeSearch` fuses a lexical (tantivy BM25) arm with an optional vector
(numpy cosine) arm via Reciprocal Rank Fusion, applies scope/tag/usage_mode filters, and
attaches strict-additive user annotations (§4). When no embedder is supplied it degrades
to lexical-only and never fails (§5.2). The lexical arm, tie-breaking, and the
embeddings-off path are deterministic, so result ordering is reproducible (§10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.knowledge.drift import DriftDetector
from canonic.knowledge.embeddings import VectorStore
from canonic.knowledge.index import KnowledgeIndex
from canonic.knowledge.loader import user_from_path
from canonic.knowledge.models import KnowledgePage, KnowledgeScope, UsageMode
from canonic.knowledge.results import Annotation, Caveat, Hit, MatchedOn, ReviewFlag, SearchResult
from canonic.knowledge.scope import ScopeResolver

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from canonic.knowledge.embeddings import Embedder
    from canonic.knowledge.validation import EntityIndex

__all__ = [
    "KnowledgeSearch",
]

#: RRF dampening constant; the standard default. Larger ``k`` flattens the contribution
#: of top ranks. ``score(doc) = Σ_arm weight[arm] / (k + rank_arm(doc))``.
_DEFAULT_RRF_K = 60

#: Default cap on auto-surfaced caveats (§8). The relevance gate for "caveat surfacing
#: volume" is an open question (§12); a small fixed cap keeps caveats from flooding results.
_DEFAULT_MAX_CAVEATS = 3

# Arms are fused in a fixed order so ``matched_on`` and tie-breaking are deterministic.
_ARM_ORDER = (MatchedOn.LEXICAL, MatchedOn.VECTOR)


def _doc_key(page: KnowledgePage) -> str:
    return f"{page.scope.value}:{page.id}"


class KnowledgeSearch:
    """Hybrid lexical + optional-vector search with RRF fusion (SPEC-E6 §5)."""

    def __init__(
        self,
        pages: Iterable[KnowledgePage],
        *,
        embedder: Embedder | None = None,
        vectors: VectorStore | None = None,
        entity_index: EntityIndex | None = None,
        rrf_k: int = _DEFAULT_RRF_K,
        weights: dict[MatchedOn, float] | None = None,
    ) -> None:
        """Build the index(es) over ``pages``.

        The lexical arm is always built. The vector arm runs only when ``embedder`` is
        supplied — its absence is the §5.2 fallback switch. If a caller already has a
        ``VectorStore`` (e.g. from a persistent cache, SPEC-E6 §5.3), pass it as
        ``vectors`` to skip rebuilding it from ``pages``; ``embedder`` is still required
        in that case to embed the live query text at search time. ``weights`` tunes each
        arm's RRF contribution (default 1.0 each). When ``entity_index`` is supplied,
        returned pages whose ``meta.bound_fingerprints`` no longer match the live measure
        definition are flagged for prose review (§7); without it no drift is computed.
        """
        self._pages: list[KnowledgePage] = list(pages)
        self._by_doc_key: dict[str, KnowledgePage] = {_doc_key(p): p for p in self._pages}
        self._index = KnowledgeIndex.build(self._pages)
        self._embedder = embedder
        self._vectors: VectorStore | None
        if vectors is not None:
            self._vectors = vectors
        elif embedder is not None:
            self._vectors = VectorStore.build(self._pages, embedder)
        else:
            self._vectors = None
        self._entity_index = entity_index
        self._drift = DriftDetector()
        self._rrf_k = rrf_k
        self._weights = weights or {MatchedOn.LEXICAL: 1.0, MatchedOn.VECTOR: 1.0}
        self._resolver = ScopeResolver()

    def search(
        self,
        query: str,
        *,
        requesting_user: str,
        tags: Sequence[str] | None = None,
        usage_mode: UsageMode | None = None,
        limit: int = 10,
        max_caveats: int = _DEFAULT_MAX_CAVEATS,
    ) -> SearchResult:
        """Run a hybrid search and return ranked hits (SPEC-E6 §5.3).

        Filters by scope (global + ``requesting_user``'s own pages), ``tags`` (a page
        passes if it shares any tag), and ``usage_mode``. Global hits carry any same-id
        user page as a strict-additive annotation (§4). Up to ``max_caveats``
        ``usage_mode: caveat`` pages whose bound entities appear in the hits ride along in
        ``SearchResult.caveats`` (§8), even when not matched by the query.
        """
        visible = self._resolver.visible_pages(requesting_user, self._pages)
        eligible = [p for p in visible if self._passes_filters(p, tags, usage_mode)]
        eligible_keys = {_doc_key(p) for p in eligible}

        # Fetch the full eligible candidate pool from each arm so fusion sees every
        # contender (the corpus is small; this keeps ordering deterministic).
        pool = max(limit, len(self._pages))
        arm_ranks = self._arm_ranks(query, eligible_keys, pool)
        ranked_keys = self._fuse(arm_ranks)

        hits = self._build_hits(ranked_keys, arm_ranks, visible, limit)
        caveats = self._surface_caveats(hits, visible, max_caveats)
        review_flags = self._review_flags(hits, caveats)
        return SearchResult(hits=hits, caveats=caveats, review_flags=review_flags)

    @staticmethod
    def _passes_filters(
        page: KnowledgePage, tags: Sequence[str] | None, usage_mode: UsageMode | None
    ) -> bool:
        if usage_mode is not None and page.usage_mode is not usage_mode:
            return False
        return not tags or bool(set(tags) & set(page.tags))

    def _arm_ranks(
        self, query: str, eligible_keys: set[str], pool: int
    ) -> dict[MatchedOn, dict[str, int]]:
        """Run each arm and return ``{arm: {doc_key: contiguous_rank}}`` over eligibles.

        Ranks are re-densified after the eligibility filter so an ineligible higher-ranked
        page never pushes an eligible one down in the RRF math.
        """
        ranks: dict[MatchedOn, dict[str, int]] = {}

        lexical = [h for h in self._index.search(query, limit=pool) if h.doc_key in eligible_keys]
        ranks[MatchedOn.LEXICAL] = {h.doc_key: rank for rank, h in enumerate(lexical)}

        if self._vectors is not None and self._embedder is not None:
            vector = [
                h
                for h in self._vectors.search(query, self._embedder, limit=pool)
                if h.doc_key in eligible_keys
            ]
            ranks[MatchedOn.VECTOR] = {h.doc_key: rank for rank, h in enumerate(vector)}

        return ranks

    def _fuse(self, arm_ranks: dict[MatchedOn, dict[str, int]]) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion → ``[(doc_key, score)]`` sorted desc, ties by page id.

        With a single arm this reduces to that arm's weighted RRF, which still ranks it
        correctly — the graceful embeddings-off path (§5.2).
        """
        keys = {key for ranks in arm_ranks.values() for key in ranks}
        scored: list[tuple[str, float]] = []
        for key in keys:
            score = sum(
                self._weights.get(arm, 1.0) / (self._rrf_k + rank)
                for arm in _ARM_ORDER
                if (rank := arm_ranks.get(arm, {}).get(key)) is not None
            )
            scored.append((key, score))
        # Sort by fused score desc, ties broken by the stable page-id key (§10) so the
        # ordering is reproducible across runs.
        scored.sort(key=lambda item: (-item[1], self._by_doc_key[item[0]].id))
        return scored

    def _build_hits(
        self,
        ranked_keys: list[tuple[str, float]],
        arm_ranks: dict[MatchedOn, dict[str, int]],
        visible: list[KnowledgePage],
        limit: int,
    ) -> list[Hit]:
        # Global pages that themselves surfaced — a colliding user page collapses into the
        # global's annotations rather than standing as its own hit (§4 strict-additive).
        global_hit_ids = {
            self._by_doc_key[key].id
            for key, _ in ranked_keys
            if self._by_doc_key[key].scope is KnowledgeScope.GLOBAL
        }
        hits: list[Hit] = []
        for key, score in ranked_keys:
            if len(hits) >= limit:
                break
            page = self._by_doc_key[key]
            if page.scope is KnowledgeScope.USER and page.id in global_hit_ids:
                continue  # surfaced as an annotation on the authoritative global hit
            matched_on = [arm for arm in _ARM_ORDER if key in arm_ranks.get(arm, {})]
            annotations = (
                self._annotations_for(page, visible) if page.scope is KnowledgeScope.GLOBAL else []
            )
            hits.append(
                Hit(
                    page=page.id,
                    scope=page.scope,
                    score=score,
                    summary=page.summary,
                    matched_on=matched_on,
                    usage_mode=page.usage_mode,
                    sl_refs=page.sl_refs,
                    annotations=annotations,
                )
            )
        return hits

    def _surface_caveats(
        self, hits: list[Hit], visible: list[KnowledgePage], max_caveats: int
    ) -> list[Caveat]:
        """Auto-surface caveat pages whose bound entities appear in the hits (§8).

        Collects the entities referenced across ``hits``, then selects visible
        ``usage_mode: caveat`` pages that bind any of them — excluding pages already returned
        as hits so a caveat that also matched the query is never duplicated. Ordered by page
        id and capped at ``max_caveats`` (the §12 relevance gate)."""
        if max_caveats <= 0:
            return []
        hit_entities = {ref for hit in hits for ref in hit.sl_refs}
        if not hit_entities:
            return []
        hit_ids = {hit.page for hit in hits}

        caveats: list[Caveat] = []
        for page in sorted(visible, key=lambda p: p.id):
            if page.usage_mode is not UsageMode.CAVEAT or page.id in hit_ids:
                continue
            triggered_by = sorted(set(page.sl_refs) & hit_entities)
            if not triggered_by:
                continue
            caveats.append(
                Caveat(
                    page=page.id,
                    scope=page.scope,
                    summary=page.summary,
                    sl_refs=page.sl_refs,
                    triggered_by=triggered_by,
                )
            )
            if len(caveats) >= max_caveats:
                break
        return caveats

    def _review_flags(self, hits: list[Hit], caveats: list[Caveat]) -> list[ReviewFlag]:
        """Flag returned pages whose bound measure definition drifted (§7, S5 AC1).

        For each distinct page surfaced as a hit or caveat, compares its
        ``meta.bound_fingerprints`` against the live :class:`EntityIndex` via
        :class:`DriftDetector`. A page with a stale binding is flagged for prose review —
        the rendered definition auto-updates, only the surrounding prose is suspect (AC2).
        Without an entity index there is nothing to compare against, so no flags are emitted.
        Returns one flag per drifted page, ordered as the pages were returned.
        """
        if self._entity_index is None:
            return []
        flags: list[ReviewFlag] = []
        seen: set[str] = set()
        served_keys = [f"{h.scope.value}:{h.page}" for h in hits]
        served_keys += [f"{c.scope.value}:{c.page}" for c in caveats]
        for doc_key in served_keys:
            if doc_key in seen:
                continue
            seen.add(doc_key)
            page = self._by_doc_key.get(doc_key)
            if page is None:
                continue
            drifted = self._drift.flagged_for_review(page, self._entity_index)
            if not drifted:
                continue
            joined = ", ".join(drifted)
            flags.append(
                ReviewFlag(
                    page=page.id,
                    scope=page.scope,
                    drifted_refs=drifted,
                    message=(
                        f"Prose review needed — referenced measure definition changed: {joined}. "
                        "The rendered definition auto-updates; only the surrounding prose is "
                        "flagged stale."
                    ),
                )
            )
        return flags

    def _annotations_for(
        self, global_page: KnowledgePage, visible: list[KnowledgePage]
    ) -> list[Annotation]:
        """Visible user pages sharing the global page's id, as strict-additive annotations."""
        annotations: list[Annotation] = []
        for page in visible:
            if page.scope is KnowledgeScope.USER and page.id == global_page.id:
                self._resolver.resolve_collision(global_page, page)  # asserts the §4 rule
                owner = user_from_path(page.path)
                annotations.append(Annotation(page=page.id, scope=f"user:{owner}"))
        return annotations
