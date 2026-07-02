"""Local embedding runtime (SPEC-E10 §5).

Powers E6's optional vector-search arm. Backed by ``sentence-transformers`` — an optional
``canonic[embeddings]`` add-on, **not** bundled by default. When the add-on is absent (or the
configured model fails to load), the runtime reports itself unavailable cleanly and E6
degrades to lexical-only search; it is never a failure (§5, S4).

The runtime satisfies E6's :class:`~canonic.knowledge.embeddings.Embedder` seam: ``embed``
turns text into vectors, and ``model_identity`` returns a fingerprint that changes whenever
the model (or anything that alters vector semantics) changes, so E6 can detect a model swap
and trigger a reindex rather than silently mixing vectors from two models.

Inherently air-gap-compatible: the backend runs locally with no egress, so no
:class:`~canonic.airgap.EgressPolicy` check is needed on the default path. A future hosted
embeddings provider (outside air-gapped mode) would reuse the same policy seam as the
generation runtime; that path is not built here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from canonic.exc import EmbeddingUnavailable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.config import EmbeddingConfig

__all__ = ["EmbeddingRuntime"]


def _load_backend(model_name: str) -> tuple[Any, str]:
    """Import ``sentence-transformers`` and load ``model_name``; raise on any failure.

    Isolated at module scope so the heavy/optional import is never performed at import time
    and so tests can substitute it. Returns the loaded model and the installed library
    version (part of the identity fingerprint).
    """
    import sentence_transformers  # type: ignore[import-not-found]  # optional add-on, no stubs

    model = sentence_transformers.SentenceTransformer(model_name)
    return model, sentence_transformers.__version__


class EmbeddingRuntime:
    """Local sentence-transformers embedding runtime (SPEC-E10 §5).

    Constructed from an :class:`~canonic.config.EmbeddingConfig`. The backend is loaded
    eagerly at construction; if the optional add-on is missing or the model cannot load,
    the failure is captured as state (not raised) and :meth:`is_available` reports false so
    E6 stays lexical-only.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._model: Any | None = None
        self._st_version: str | None = None
        self._unavailable_reason: str | None = None
        try:
            # Broad on purpose: a missing add-on (ImportError), a missing/corrupt model, or
            # any load-time failure must degrade to "unavailable", never crash a host that
            # opted out of embeddings (§5, S4).
            self._model, self._st_version = _load_backend(config.model)
        except Exception as exc:
            self._unavailable_reason = str(exc)

    def is_available(self) -> bool:
        """Whether the backend loaded and embeddings can be produced (SPEC-E10 §5, S4).

        E6 reads this as the §5.2 fallback switch: false ⇒ lexical-only search.
        """
        return self._model is not None

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Embed ``texts`` into an ``(n, d)`` float32 matrix, one row per input.

        Raises:
            EmbeddingUnavailable: The backend is not available; callers must gate on
                :meth:`is_available` first (E6 does, via the injected-embedder switch).
        """
        if self._model is None:
            raise EmbeddingUnavailable(
                f"embedding backend unavailable for model {self._config.model!r}: "
                f"{self._unavailable_reason}"
            )
        vectors = self._model.encode(list(texts), convert_to_numpy=True)
        return np.asarray(vectors, dtype=np.float32)

    def model_identity(self) -> str:
        """Fingerprint identifying the active model (SPEC-E10 §5).

        Combines the model name, its output dimension, and the installed
        ``sentence-transformers`` version, so any change that alters vector semantics changes
        the fingerprint. E6 compares it against a store's recorded identity to decide whether
        a reindex is needed (never mixing vectors from two models).

        Raises:
            EmbeddingUnavailable: The backend is not available, so there is no identity.
        """
        if self._model is None:
            raise EmbeddingUnavailable(
                f"embedding backend unavailable for model {self._config.model!r}: "
                f"{self._unavailable_reason}"
            )
        dim = self._model.get_sentence_embedding_dimension()
        return f"sentence-transformers/{self._config.model}@dim={dim}@stv={self._st_version}"
