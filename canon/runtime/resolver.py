"""Task → model resolution for the generation runtime (SPEC-E10 §3)."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canon.config import LLMConfig


class Task(StrEnum):
    """A named generation task that routes to a model (SPEC-E10 §3).

    The v1 task set: ``draft`` (E4 builder) and ``reconcile`` (E4 reconciliation). A
    ``StrEnum`` member *is* its lowercase string, so it indexes ``llm.tasks`` (keyed by the
    YAML strings) directly and serializes without conversion.
    """

    DRAFT = "draft"
    RECONCILE = "reconcile"


def resolve_model(config: LLMConfig, task: Task | None) -> str:
    """Resolve a named task to its configured model, falling back to the default.

    A task with an entry in ``llm.tasks`` uses that model; a task with no override uses the
    default ``llm.model``. Resolution is deterministic: task → override or default.

    NOTE (#62): fallback-to-default for an un-overridden task is the documented §3 contract,
    not a silent swap. The no-silent-substitution guarantee — never quietly switching to a
    *different* model when a call fails — lives on the failure path in ``GenerationRuntime``
    (bounded retries on the same model, then a structured ``GenerationError``), not here.
    """
    if task is None:
        return config.model
    return config.tasks.get(task, config.model)
