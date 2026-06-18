"""Task → model resolution for the generation runtime (SPEC-E10 §3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canon.config import LLMConfig


def resolve_model(config: LLMConfig, task: str | None) -> str:
    """Resolve a named task to its configured model, falling back to the default.

    A task with an entry in ``llm.tasks`` uses that model; anything else uses the default
    ``llm.model``.

    NOTE (#62): this is the minimal resolver. Task-based routing (#62) hardens it into a
    no-silent-substitution contract — an unmapped task there is an explicit error rather
    than a quiet fallback, since a silent model swap changes behavior invisibly.
    """
    if task is None:
        return config.model
    return config.tasks.get(task, config.model)
