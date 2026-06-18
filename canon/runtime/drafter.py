"""E10-backed LLM drafter for the E4 builder seam (SPEC-E10 §10, SPEC-E4 §4).

Bridges the deterministic builder's :class:`~canon.ingestion.builder.LLMDrafter` seam to a
real :class:`~canon.runtime.generation.GenerationRuntime`. Injected on the interactive path
to replace the headless ``NullLLMDrafter``; this is the concrete proof of SPEC-E10 S1-AC1
— an E4 draft succeeds with no engine-specific code path.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from canon.ingestion.builder import LLM_GRAIN_CONFIDENCE, GrainDraft
from canon.runtime.resolver import Task

if TYPE_CHECKING:
    from canon.connectors.base import RelationSchema
    from canon.runtime.generation import GenerationRuntime

__all__ = ["RuntimeLLMDrafter"]

_GRAIN_SYSTEM = (
    "You infer the grain (the minimal set of columns that uniquely identifies a row) of a "
    "database relation that declares no primary key. Respond only with the requested JSON."
)


class _GrainResponse(BaseModel):
    """Schema the model must satisfy when drafting a grain."""

    grain: list[str]


class RuntimeLLMDrafter:
    """A real ``LLMDrafter`` backed by the generation runtime (SPEC-E10 S1-AC1).

    Satisfies the sync ``LLMDrafter`` Protocol by bridging to the async runtime with
    ``asyncio.run``. That bridge is for the interactive/CLI path, which is not already
    inside an event loop; the headless ingestion pipeline keeps ``NullLLMDrafter`` and
    stays fully deterministic (SPEC-E4 §9).

    NOTE (#67): the full runtime interface may invert this into an async drafter Protocol,
    removing the bridge.
    """

    def __init__(self, runtime: GenerationRuntime) -> None:
        self._runtime = runtime

    def draft_grain(self, schema: RelationSchema) -> GrainDraft:
        """Propose a grain for a relation with no declared primary key."""
        completion = asyncio.run(
            self._runtime.generate(
                _grain_prompt(schema),
                task=Task.DRAFT,
                system=_GRAIN_SYSTEM,
                response_model=_GrainResponse,
            )
        )
        grain = completion.parsed["grain"] if completion.parsed else []
        return GrainDraft(grain=grain, confidence=LLM_GRAIN_CONFIDENCE)

    def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ARG002
        """Propose joins from observed-query evidence.

        Deferred to a later E4 stage; grain drafting alone exercises the seam for #61.
        """
        return []


def _grain_prompt(schema: RelationSchema) -> str:
    """Render the relation's columns into a grain-inference prompt."""
    columns = "\n".join(
        f"- {c.name} ({c.type}{', nullable' if c.nullable else ''})" for c in schema.columns
    )
    return (
        f"Relation {schema.relation!r} has these columns:\n{columns}\n\n"
        'Return the grain as a JSON object {"grain": [<column names>]}.'
    )
