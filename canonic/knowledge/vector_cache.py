"""Persistent, content-hash-keyed cache for the vector arm (SPEC-E6 §5.1, §5.3).

``VectorStore.build`` re-embeds every knowledge page on every call, which is fine for the
lexical arm but not for embeddings: re-running the model over the whole knowledge base on
every single search is wasted work once the corpus is more than a handful of pages. This
cache persists page vectors under ``.canonic/`` (SPEC-E6 §5.3: "the index lives under
``.canonic/`` ... refreshed on ingest or page change") and only re-embeds pages whose
content actually changed since the last search, keyed by a fingerprint of the exact text
``VectorStore.build`` embeds.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import numpy as np

from canonic.knowledge.embeddings import VectorStore

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.knowledge.embeddings import Embedder
    from canonic.knowledge.models import KnowledgePage, KnowledgeScope

__all__ = ["VectorIndexCache"]


def _page_text(page: KnowledgePage) -> str:
    # Must match VectorStore.build exactly, so a fingerprint hit guarantees the cached
    # vector is what re-embedding would produce.
    return f"{page.summary}\n{page.body}"


def _fingerprint(page: KnowledgePage) -> str:
    digest = hashlib.sha256(_page_text(page).encode()).hexdigest()
    return f"sha256:{digest}"


def _doc_key(page: KnowledgePage) -> str:
    return f"{page.scope.value}:{page.id}"


class VectorIndexCache:
    """Loads/persists page embeddings at ``cache_path``, re-embedding only what changed."""

    def __init__(self, cache_path: Path) -> None:
        self._cache_path = cache_path

    def _load(self) -> tuple[str | None, dict[str, dict[str, object]]]:
        """Return ``(model_identity, entries_by_doc_key)``, or ``(None, {})`` on any failure.

        Broad on purpose: a missing, truncated, or hand-edited cache file must degrade to
        "start fresh", never crash a search (same posture as ``EmbeddingRuntime``).
        """
        try:
            raw = json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return None, {}
        entries = raw.get("entries")
        model_identity = raw.get("model_identity")
        if model_identity is None or not isinstance(entries, list):
            return None, {}
        by_key = {e["doc_key"]: e for e in entries if isinstance(e, dict) and "doc_key" in e}
        return model_identity, by_key

    def _save(
        self,
        *,
        model_identity: str,
        doc_keys: list[str],
        ids: list[str],
        scopes: list[KnowledgeScope],
        fingerprints: list[str],
        matrix: np.ndarray,
    ) -> None:
        entries = [
            {
                "doc_key": doc_keys[i],
                "id": ids[i],
                "scope": scopes[i].value,
                "fingerprint": fingerprints[i],
                "vector": matrix[i].tolist(),
            }
            for i in range(len(doc_keys))
        ]
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps({"model_identity": model_identity, "entries": entries})
        )

    def load_or_build(self, pages: list[KnowledgePage], embedder: Embedder) -> VectorStore:
        """Return a ``VectorStore`` for ``pages``, re-embedding only new/changed content.

        Pages whose fingerprint matches a cached entry reuse the cached (already
        L2-normalized) vector; everything else is embedded in one batched call. The result
        is written back so the next call sees an up-to-date cache. Pages no longer present
        are dropped from what gets persisted — self-pruning, no explicit invalidation
        needed. A model-identity change (or an unreadable cache) discards everything and
        re-embeds the full corpus, matching ``VectorStore.needs_reindex``.
        """
        model_identity = embedder.model_identity()
        cached_model_identity, cached = self._load()
        if cached_model_identity != model_identity:
            cached = {}

        doc_keys = [_doc_key(p) for p in pages]
        fingerprints = [_fingerprint(p) for p in pages]

        to_embed_idx: list[int] = []
        to_embed_texts: list[str] = []
        vectors: list[np.ndarray | None] = [None] * len(pages)

        dim = None
        for i, page in enumerate(pages):
            entry = cached.get(doc_keys[i])
            if entry is not None and entry.get("fingerprint") == fingerprints[i]:
                vec = np.asarray(entry["vector"], dtype=np.float32)
                vectors[i] = vec
                dim = vec.shape[0]
            else:
                to_embed_idx.append(i)
                to_embed_texts.append(_page_text(page))

        if to_embed_texts:
            embedded = np.asarray(embedder.embed(to_embed_texts), dtype=np.float32)
            norms = np.linalg.norm(embedded, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            embedded = embedded / norms
            for j, i in enumerate(to_embed_idx):
                vectors[i] = embedded[j]
                dim = embedded.shape[1]

        if not pages:
            matrix = np.zeros((0, 0), dtype=np.float32)
        else:
            matrix = np.zeros((len(pages), dim or 0), dtype=np.float32)
            for i, stored_vec in enumerate(vectors):
                if stored_vec is not None:
                    matrix[i] = stored_vec

        ids = [p.id for p in pages]
        scopes = [p.scope for p in pages]
        self._save(
            model_identity=model_identity,
            doc_keys=doc_keys,
            ids=ids,
            scopes=scopes,
            fingerprints=fingerprints,
            matrix=matrix,
        )
        return VectorStore(matrix, doc_keys, ids, scopes, model_identity)
