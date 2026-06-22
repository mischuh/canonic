"""E10-backed LLM drafter for the E4 builder seam (SPEC-E10 §10, SPEC-E4 §4).

Bridges the deterministic builder's :class:`~canon.ingestion.builder.LLMDrafter` seam to a
real :class:`~canon.runtime.generation.GenerationRuntime`. Injected on the interactive path
to replace the headless ``NullLLMDrafter``; this is the concrete proof of SPEC-E10 S1-AC1
— an E4 draft succeeds with no engine-specific code path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from canon.ingestion.builder import LLM_GRAIN_CONFIDENCE, GrainDraft, NullLLMDrafter
from canon.ingestion.reconciliation import NullReconcileDrafter, ResolutionDraft
from canon.runtime.resolver import Task

if TYPE_CHECKING:
    from canon.airgap import EgressPolicy
    from canon.config import LLMConfig, RuntimeConfig
    from canon.connectors.base import RelationSchema
    from canon.ingestion.builder import LLMDrafter
    from canon.ingestion.models import Proposal
    from canon.ingestion.reconciliation import ReconcileDrafter
    from canon.runtime.generation import GenerationRuntime

__all__ = ["RuntimeLLMDrafter", "RuntimeReconcileDrafter", "make_drafter", "make_reconcile_drafter"]

_GRAIN_SYSTEM = (
    "You infer the grain (the minimal set of columns that uniquely identifies a row) of a "
    "database relation that declares no primary key. Respond only with the requested JSON."
)


class _GrainResponse(BaseModel):
    """Schema the model must satisfy when drafting a grain."""

    grain: list[str]


class RuntimeLLMDrafter:
    """A real ``LLMDrafter`` backed by the generation runtime (SPEC-E10 S1-AC1).

    Satisfies the async ``LLMDrafter`` Protocol by delegating directly to the async runtime.
    Injected on the interactive path to replace the headless ``NullLLMDrafter``; the headless
    pipeline stays fully deterministic (SPEC-E4 §9).
    """

    def __init__(self, runtime: GenerationRuntime) -> None:
        self._runtime = runtime

    async def draft_grain(self, schema: RelationSchema) -> GrainDraft:
        """Propose a grain for a relation with no declared primary key."""
        completion = await self._runtime.generate(
            _grain_prompt(schema),
            task=Task.DRAFT,
            system=_GRAIN_SYSTEM,
            response_model=_GrainResponse,
        )
        grain = completion.parsed["grain"] if completion.parsed else []
        return GrainDraft(grain=grain, confidence=LLM_GRAIN_CONFIDENCE)

    async def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: ARG002
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


_RECONCILE_SYSTEM = (
    "You resolve contradictions between two proposed descriptions of the same database object. "
    "Given two proposals, select the one that is most accurate and complete. "
    "Respond only with the requested JSON."
)


class _ResolutionResponse(BaseModel):
    """Schema the model must satisfy when resolving a contradiction."""

    winner_index: int


class RuntimeReconcileDrafter:
    """A real ``ReconcileDrafter`` backed by the generation runtime (SPEC-E10 S2-AC1).

    Presents the conflicting proposals to the stronger ``reconcile``-task model and returns
    the winning index. Injected on the interactive path to replace ``NullReconcileDrafter``.
    """

    def __init__(self, runtime: GenerationRuntime) -> None:
        self._runtime = runtime

    async def draft_resolution(
        self, target: str, proposals: list[Proposal]
    ) -> ResolutionDraft | None:
        """Ask the model to pick the winning proposal for a contradicting group."""
        completion = await self._runtime.generate(
            _resolution_prompt(target, proposals),
            task=Task.RECONCILE,
            system=_RECONCILE_SYSTEM,
            response_model=_ResolutionResponse,
        )
        if not completion.parsed:
            return None
        winner_index = completion.parsed.get("winner_index")
        if not isinstance(winner_index, int) or not (0 <= winner_index < len(proposals)):
            return None
        return ResolutionDraft(winner_index=winner_index)


def _resolution_prompt(target: str, proposals: list[Proposal]) -> str:
    """Render the conflicting proposals into a resolution prompt."""
    lines = [f"Two sources disagree on the description of {target!r}.", ""]
    for i, proposal in enumerate(proposals):
        lines.append(f"Proposal {i}:")
        lines.append(json.dumps(proposal.content, indent=2, default=str))
        lines.append("")
    lines.append(
        'Return the index of the better proposal as JSON: {"winner_index": 0} or {"winner_index": 1}.'
    )
    return "\n".join(lines)


def make_drafter(
    llm: LLMConfig | None,
    runtime: RuntimeConfig,
    *,
    headless: bool,
) -> LLMDrafter:
    """Return the right LLMDrafter for the operating mode (SPEC-E10 §9).

    Headless or no LLM configured → NullLLMDrafter (zero model calls, fully deterministic).
    Interactive with LLM → RuntimeLLMDrafter backed by GenerationRuntime.
    Air-gapped policy is threaded into the runtime so the egress guard fires at
    construction time (before any call) even in interactive mode.
    """
    if headless or llm is None:
        return NullLLMDrafter()
    from canon.airgap import EgressPolicy
    from canon.runtime.generation import GenerationRuntime

    policy: EgressPolicy | None = (
        EgressPolicy(allow_cidrs=runtime.allow_cidrs) if runtime.air_gapped else None
    )
    return RuntimeLLMDrafter(GenerationRuntime(llm, policy=policy))


def make_reconcile_drafter(
    llm: LLMConfig | None,
    runtime: RuntimeConfig,
    *,
    headless: bool,
) -> ReconcileDrafter:
    """Return the right ReconcileDrafter for the operating mode (SPEC-E10 §9).

    Headless or no LLM configured → NullReconcileDrafter (no model calls).
    Interactive with LLM → RuntimeReconcileDrafter backed by GenerationRuntime.
    """
    if headless or llm is None:
        return NullReconcileDrafter()
    from canon.airgap import EgressPolicy
    from canon.runtime.generation import GenerationRuntime

    policy: EgressPolicy | None = (
        EgressPolicy(allow_cidrs=runtime.allow_cidrs) if runtime.air_gapped else None
    )
    return RuntimeReconcileDrafter(GenerationRuntime(llm, policy=policy))
