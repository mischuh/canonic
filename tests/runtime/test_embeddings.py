"""Tests for canonic/runtime/embeddings.py — the local embedding runtime (SPEC-E10 §5, GH-64).

No mock library and no model download: the optional ``sentence-transformers`` backend is
substituted by patching the module-level ``_load_backend`` seam, matching the codebase's
Fake-implementation style.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from canonic.config import EmbeddingConfig
from canonic.exc import EmbeddingUnavailable
from canonic.runtime import embeddings as embeddings_module
from canonic.runtime.embeddings import EmbeddingRuntime

if TYPE_CHECKING:
    from collections.abc import Sequence


class _FakeModel:
    """Stand-in for a loaded SentenceTransformer exposing the two methods used."""

    def __init__(self, dim: int = 3) -> None:
        self._dim = dim

    def encode(self, texts: Sequence[str], *, convert_to_numpy: bool = True) -> np.ndarray:
        # One deterministic row per text; values don't matter for these tests.
        return np.arange(len(texts) * self._dim, dtype=np.float64).reshape(len(texts), self._dim)

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    """Patch ``_load_backend`` to return a fake model + version (no import / download)."""
    model = _FakeModel(dim=3)

    def fake_load(model_name: str) -> tuple[_FakeModel, str]:
        return model, "9.9.9"

    monkeypatch.setattr(embeddings_module, "_load_backend", fake_load)
    return model


# --- backend absent: graceful degradation (S4) --------------------------------


def test_unavailable_when_backend_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(model_name: str) -> tuple[object, str]:
        raise ImportError("No module named 'sentence_transformers'")

    monkeypatch.setattr(embeddings_module, "_load_backend", boom)

    # Construction must not raise even though the add-on is absent.
    runtime = EmbeddingRuntime(EmbeddingConfig())
    assert runtime.is_available() is False


def test_embed_while_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        embeddings_module, "_load_backend", lambda name: (_ for _ in ()).throw(ImportError("nope"))
    )
    runtime = EmbeddingRuntime(EmbeddingConfig())
    with pytest.raises(EmbeddingUnavailable, match="unavailable"):
        runtime.embed(["hello"])
    with pytest.raises(EmbeddingUnavailable):
        runtime.model_identity()


def test_model_load_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-import failure (e.g. a missing/corrupt model) also degrades cleanly.
    def boom(model_name: str) -> tuple[object, str]:
        raise OSError("model files not found")

    monkeypatch.setattr(embeddings_module, "_load_backend", boom)
    assert EmbeddingRuntime(EmbeddingConfig()).is_available() is False


# --- backend present ----------------------------------------------------------


def test_is_available_when_backend_loads(fake_backend: _FakeModel) -> None:
    assert EmbeddingRuntime(EmbeddingConfig()).is_available() is True


def test_embed_returns_float32_matrix(fake_backend: _FakeModel) -> None:
    runtime = EmbeddingRuntime(EmbeddingConfig())
    vectors = runtime.embed(["a", "b"])
    assert vectors.shape == (2, 3)
    assert vectors.dtype == np.float32


def test_model_identity_carries_model_dim_and_version(fake_backend: _FakeModel) -> None:
    runtime = EmbeddingRuntime(EmbeddingConfig(model="my-model"))
    identity = runtime.model_identity()
    assert "my-model" in identity
    assert "dim=3" in identity
    assert "stv=9.9.9" in identity


def test_model_identity_changes_with_model(fake_backend: _FakeModel) -> None:
    a = EmbeddingRuntime(EmbeddingConfig(model="model-a")).model_identity()
    b = EmbeddingRuntime(EmbeddingConfig(model="model-b")).model_identity()
    assert a != b
