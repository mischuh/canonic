"""Vector arm: the embeddings seam + a numpy cosine store (SPEC-E6 §5.1, optional).

The vector arm is **optional** — it runs only when an embedding runtime is installed
(E10). E6 ships no concrete embedder: it defines the :class:`Embedder` seam E10 plugs
into and the numpy cosine store that ranks pages once embeddings exist. When no embedder
is supplied, search falls back to lexical-only and never fails (§5.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from canonic.knowledge.models import KnowledgePage, KnowledgeScope

__all__ = [
    "Embedder",
    "VectorHit",
    "VectorStore",
]


@runtime_checkable
class Embedder(Protocol):
    """Turns text into embedding vectors. The E10 hand-off point.

    Implementations return a ``(n, d)`` float matrix, one row per input text. E6 never
    constructs one; the caller injects it only when embeddings are installed.

    An embedder is also *identified*: :meth:`model_identity` returns a fingerprint that
    changes whenever the underlying model (or anything that alters vector semantics) changes.
    The store captures it at build time so E6 can detect a model swap and trigger a reindex
    rather than silently mixing vectors from two models (SPEC-E10 §5).
    """

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...

    def model_identity(self) -> str: ...


class VectorHit(NamedTuple):
    """One ranked vector candidate. ``rank`` is 0-based; ``score`` is cosine similarity."""

    doc_key: str
    id: str
    scope: KnowledgeScope
    score: float
    rank: int


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Row-normalize so a dot product is cosine similarity; zero rows stay zero."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    normalized: np.ndarray = matrix / norms
    return normalized


class VectorStore:
    """Page embeddings in a numpy matrix, ranked by cosine similarity (SPEC-E6 §5.1)."""

    def __init__(
        self,
        matrix: np.ndarray,
        doc_keys: list[str],
        ids: list[str],
        scopes: list[KnowledgeScope],
        model_identity: str,
    ) -> None:
        # Construct via :meth:`build`; ``matrix`` rows are pre-normalized.
        self._matrix = matrix
        self._doc_keys = doc_keys
        self._ids = ids
        self._scopes = scopes
        # Fingerprint of the embedder that produced ``matrix``. A store only ever holds
        # vectors from one model — :meth:`needs_reindex` guards against mixing (§5).
        self._model_identity = model_identity

    @property
    def model_identity(self) -> str:
        """Fingerprint of the embedder whose vectors this store holds (SPEC-E10 §5)."""
        return self._model_identity

    def needs_reindex(self, embedder: Embedder) -> bool:
        """Whether ``embedder`` differs from the one that built this store.

        ``True`` means the active embedding model changed since these vectors were computed,
        so E6 must rebuild rather than search a stale store — vectors from two models are
        never mixed (SPEC-E10 §5, S4).
        """
        return self._model_identity != embedder.model_identity()

    @classmethod
    def build(cls, pages: Iterable[KnowledgePage], embedder: Embedder) -> VectorStore:
        """Embed each page (``summary`` + ``body``) into the store."""
        pages = list(pages)
        identity = embedder.model_identity()
        doc_keys = [f"{p.scope.value}:{p.id}" for p in pages]
        ids = [p.id for p in pages]
        scopes = [p.scope for p in pages]
        if not pages:
            return cls(np.zeros((0, 0), dtype=np.float32), [], [], [], identity)
        texts = [f"{p.summary}\n{p.body}" for p in pages]
        matrix = _l2_normalize(np.asarray(embedder.embed(texts), dtype=np.float32))
        return cls(matrix, doc_keys, ids, scopes, identity)

    def search(self, query: str, embedder: Embedder, *, limit: int) -> list[VectorHit]:
        """Return up to ``limit`` cosine-ranked candidates for ``query``.

        Empty corpus or blank query yields an empty list rather than raising (§5.2).
        """
        if self._matrix.shape[0] == 0 or not query.strip():
            return []
        q = _l2_normalize(np.asarray(embedder.embed([query]), dtype=np.float32))[0]
        sims = self._matrix @ q
        # A non-positive cosine is not a "hit" — an orthogonal/opposed page should not be
        # recorded as matched_on=vector. Ties in similarity break by the stable page id so
        # ranks (and fusion) are reproducible (§10).
        candidates = sorted(
            (i for i in range(sims.shape[0]) if sims[i] > 0.0),
            key=lambda i: (-float(sims[i]), self._ids[i]),
        )[:limit]
        return [
            VectorHit(
                doc_key=self._doc_keys[i],
                id=self._ids[i],
                scope=self._scopes[i],
                score=float(sims[i]),
                rank=rank,
            )
            for rank, i in enumerate(candidates)
        ]
