"""E10 LLM & Embeddings runtime — turns the configured ``llm`` block into model calls.

#61 lands the generation half: a litellm-backed :class:`GenerationRuntime` over the
``openai_compatible`` provider, plus :class:`Completion`. #64 adds the local
:class:`EmbeddingRuntime` (sentence-transformers) that powers E6's vector arm. #67
completes the public interface: :class:`Usage` metrics (tokens/calls/latency) on every
:class:`Completion`, and the full structured-error taxonomy including
:exc:`~canon.exc.RetriesExhausted` for timeout-after-retries.
"""

from __future__ import annotations

from canon.runtime.embeddings import EmbeddingRuntime
from canon.runtime.generation import GenerationRuntime
from canon.runtime.models import Completion, Usage

__all__ = ["Completion", "EmbeddingRuntime", "GenerationRuntime", "Usage"]
