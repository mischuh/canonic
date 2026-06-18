"""E10 LLM & Embeddings runtime — turns the configured ``llm`` block into model calls.

#61 lands the generation half: a litellm-backed :class:`GenerationRuntime` over the
``openai_compatible`` provider, plus :class:`Completion`. The embedding runtime (#64) and
the full interface / usage metrics (#67) land beside these as peers.
"""

from __future__ import annotations

from canon.runtime.generation import GenerationRuntime
from canon.runtime.models import Completion

__all__ = ["Completion", "GenerationRuntime"]
