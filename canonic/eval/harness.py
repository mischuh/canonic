"""The baseline harness: run candidate models through the real ``draft`` path (SPEC-E10 §7).

``run_baseline`` drives :class:`~canonic.runtime.drafter.RuntimeLLMDrafter` (the production grain
drafter) over each candidate and the labeled set, classifying every run into a
:class:`~canonic.eval.models.StructuredOutcome` and scoring its grain. Reusing the real drafter —
not a re-implemented prompt — is what makes the baseline measure behavior E4 actually sees.

Usage is read *in-harness* (SPEC-E10 §7 decision; does not depend on #67): latency is timed with
:func:`time.perf_counter`, and per-call token counts are read best-effort from the litellm
response via a scoped success callback (:class:`LiteLLMUsageReader`). Token counts are advisory —
``None`` when the backend does not report them — so they never gate a run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol

from canonic.eval.models import BaselineReport, CaseOutcome, ModelTaskSummary, StructuredOutcome
from canonic.eval.scoring import score_grain, score_resolution, summarize
from canonic.exc import (
    CredentialError,
    GenerationError,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canonic.runtime.resolver import Task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from canonic.config import LLMConfig
    from canonic.connectors.base import RelationSchema
    from canonic.eval.candidates import NamedCandidate
    from canonic.eval.dataset import GrainCase, ReconcileCase
    from canonic.ingestion.builder import GrainDraft
    from canonic.ingestion.reconciliation import ResolutionDraft

__all__ = ["GrainDrafter", "LiteLLMUsageReader", "ReconcileDrafter", "UsageReader", "run_baseline"]

#: Default share of cases a candidate must HONOR (schema-valid output) to be recommendable.
DEFAULT_ADHERENCE_FLOOR = 0.9


class GrainDrafter(Protocol):
    """The production grain-drafting seam the harness measures (RuntimeLLMDrafter satisfies it)."""

    async def draft_grain(self, schema: RelationSchema) -> GrainDraft: ...


class ReconcileDrafter(Protocol):
    """The production reconcile-resolution seam (RuntimeReconcileDrafter satisfies it)."""

    async def draft_resolution(
        self, target: str, proposals: list[dict[str, Any]]
    ) -> ResolutionDraft | None: ...


class UsageReader(Protocol):
    """A scoped probe that reports the token count of the most recent generation call.

    Used as a context manager around the whole run; ``reset`` is called before each case and
    ``last_total_tokens`` read after. Implementations are best-effort — ``None`` is always valid.
    """

    def __enter__(self) -> UsageReader: ...
    def __exit__(self, *exc: object) -> None: ...
    def reset(self) -> None: ...
    @property
    def last_total_tokens(self) -> int | None: ...


def _default_drafter_factory(config: LLMConfig) -> GrainDrafter:
    """Build the real generation-backed drafter for a candidate (the production path)."""
    from canonic.runtime.drafter import RuntimeLLMDrafter
    from canonic.runtime.generation import GenerationRuntime

    return RuntimeLLMDrafter(GenerationRuntime(config))


class _HarnessReconcileAdapter:
    """Adapts the generation runtime to the harness ReconcileDrafter seam.

    The harness passes raw proposal dicts from ReconcileCase; the engine's
    RuntimeReconcileDrafter expects list[Proposal]. This adapter builds the prompt
    directly from dicts so the two seams stay decoupled.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    async def draft_resolution(
        self, target: str, proposals: list[dict[str, Any]]
    ) -> ResolutionDraft | None:
        import json

        from canonic.ingestion.reconciliation import ResolutionDraft as _RD
        from canonic.runtime.resolver import Task

        lines = [f"Two sources disagree on the description of {target!r}.", ""]
        for i, proposal in enumerate(proposals):
            lines.append(f"Proposal {i}:")
            lines.append(json.dumps(proposal, indent=2, default=str))
            lines.append("")
        lines.append(
            'Return the index of the better proposal as JSON: {"winner_index": 0} or {"winner_index": 1}.'
        )
        prompt = "\n".join(lines)

        from pydantic import BaseModel

        class _Resp(BaseModel):
            winner_index: int

        system = (
            "You resolve contradictions between two proposed descriptions of the same database object. "
            "Given two proposals, select the one that is most accurate and complete. "
            "Respond only with the requested JSON."
        )
        completion = await self._runtime.generate(
            prompt, task=Task.RECONCILE, system=system, response_model=_Resp
        )
        if not completion.parsed:
            return None
        winner_index = completion.parsed.get("winner_index")
        if not isinstance(winner_index, int) or not (0 <= winner_index < len(proposals)):
            return None
        return _RD(winner_index=winner_index)


def _default_reconcile_drafter_factory(config: LLMConfig) -> ReconcileDrafter:
    """Build the real generation-backed reconcile drafter for a candidate."""
    from canonic.runtime.generation import GenerationRuntime

    return _HarnessReconcileAdapter(GenerationRuntime(config))


class LiteLLMUsageReader:
    """Best-effort token reader: a scoped litellm success callback (SPEC-E10 §7).

    Appends a recorder to ``litellm.success_callback`` for the duration of the run and restores
    the prior list on exit, so it never leaks global state. Reads ``total_tokens`` from the
    litellm response; any shape it cannot read yields ``None`` rather than failing the run.
    """

    def __init__(self) -> None:
        self._tokens: int | None = None
        self._litellm: Any = None
        self._previous: list[Any] | None = None

    def __enter__(self) -> LiteLLMUsageReader:
        import litellm

        self._litellm = litellm
        self._previous = list(getattr(litellm, "success_callback", []) or [])
        litellm.success_callback = [*self._previous, self._record]
        return self

    def __exit__(self, *exc: object) -> None:
        if self._litellm is not None and self._previous is not None:
            self._litellm.success_callback = self._previous

    def _record(self, _kwargs: Any, response: Any, _start: Any, _end: Any) -> None:
        try:
            self._tokens = int(response.usage.total_tokens)
        except (AttributeError, TypeError, ValueError):
            self._tokens = None

    def reset(self) -> None:
        self._tokens = None

    @property
    def last_total_tokens(self) -> int | None:
        return self._tokens


async def _run_case(drafter: GrainDrafter, case: GrainCase, reader: UsageReader) -> CaseOutcome:
    """Run one labeled case through the drafter, classifying the structured-output outcome."""
    schema = case.to_schema()
    reader.reset()
    start = perf_counter()
    try:
        draft = await drafter.draft_grain(schema)
    except StructuredOutputUnsupported as exc:
        return _failure(case, StructuredOutcome.UNSUPPORTED, start, exc)
    except StructuredOutputError as exc:
        return _failure(case, StructuredOutcome.SCHEMA_INVALID, start, exc)
    except (GenerationError, CredentialError) as exc:
        return _failure(case, StructuredOutcome.ERROR, start, exc)

    latency_ms = (perf_counter() - start) * 1000
    return CaseOutcome(
        relation=case.relation,
        correct=score_grain(draft.grain, case.expected_grain),
        structured=StructuredOutcome.HONORED,
        latency_ms=latency_ms,
        total_tokens=reader.last_total_tokens,
        predicted_grain=list(draft.grain),
        expected_grain=list(case.expected_grain),
    )


def _failure(
    case: GrainCase, outcome: StructuredOutcome, start: float, exc: Exception
) -> CaseOutcome:
    """A non-HONORED outcome: never correct, but still timed."""
    return CaseOutcome(
        relation=case.relation,
        correct=False,
        structured=outcome,
        latency_ms=(perf_counter() - start) * 1000,
        expected_grain=list(case.expected_grain),
        error=str(exc),
    )


async def _run_reconcile_case(
    drafter: ReconcileDrafter, case: ReconcileCase, reader: UsageReader
) -> CaseOutcome:
    """Run one labeled reconcile case through the drafter, classifying the outcome."""
    reader.reset()
    start = perf_counter()
    try:
        draft = await drafter.draft_resolution(case.target, case.proposals)
    except StructuredOutputUnsupported as exc:
        return _reconcile_failure(case, StructuredOutcome.UNSUPPORTED, start, exc)
    except StructuredOutputError as exc:
        return _reconcile_failure(case, StructuredOutcome.SCHEMA_INVALID, start, exc)
    except (GenerationError, CredentialError) as exc:
        return _reconcile_failure(case, StructuredOutcome.ERROR, start, exc)

    latency_ms = (perf_counter() - start) * 1000
    predicted = draft.winner_index if draft is not None else -1
    return CaseOutcome(
        relation=case.target,
        correct=score_resolution(predicted, case.expected_winner),
        structured=StructuredOutcome.HONORED,
        latency_ms=latency_ms,
        total_tokens=reader.last_total_tokens,
        predicted_grain=[str(predicted)],
        expected_grain=[str(case.expected_winner)],
    )


def _reconcile_failure(
    case: ReconcileCase, outcome: StructuredOutcome, start: float, exc: Exception
) -> CaseOutcome:
    return CaseOutcome(
        relation=case.target,
        correct=False,
        structured=outcome,
        latency_ms=(perf_counter() - start) * 1000,
        expected_grain=[str(case.expected_winner)],
        error=str(exc),
    )


def _recommend(summaries: Sequence[ModelTaskSummary], adherence_floor: float) -> str | None:
    """Recommend the most accurate candidate that also clears the structured-output floor.

    Adherence gates first: a model that returns unparseable output is unusable for E4 however
    accurate its rare parseable answers are. Latency breaks ties (lower wins).
    """
    eligible = [s for s in summaries if s.schema_adherence >= adherence_floor]
    if not eligible:
        return None
    best = max(eligible, key=lambda s: (s.accuracy, -s.p50_latency_ms))
    return best.name


async def run_baseline(
    candidates: Sequence[NamedCandidate],
    cases: Sequence[GrainCase] | Sequence[ReconcileCase],
    *,
    task: Task = Task.DRAFT,
    drafter_factory: Callable[[LLMConfig], GrainDrafter]
    | Callable[[LLMConfig], ReconcileDrafter] = _default_drafter_factory,
    usage_reader: UsageReader | None = None,
    adherence_floor: float = DEFAULT_ADHERENCE_FLOOR,
    now: datetime | None = None,
) -> BaselineReport:
    """Run every candidate over the labeled set and build the per-task baseline report.

    Args:
        candidates: Models to evaluate (a friendly name + resolved ``LLMConfig`` each).
        cases: The labeled set — grain cases for ``draft``, reconcile cases for ``reconcile``.
        task: The task this baseline covers (``draft`` or ``reconcile``).
        drafter_factory: Builds the drafter for a candidate; defaults to the real
            generation-backed drafter. Injected so tests run without a network.
        usage_reader: Token probe; defaults to :class:`LiteLLMUsageReader`. Injected for tests.
        adherence_floor: Minimum structured-output adherence to be recommendable.
        now: Override for ``generated_at`` (tests); defaults to current UTC.
    """
    reader = usage_reader if usage_reader is not None else LiteLLMUsageReader()
    summaries: list[ModelTaskSummary] = []
    with reader:
        for candidate in candidates:
            drafter = drafter_factory(candidate.config)
            if task is Task.RECONCILE:
                outcomes = [
                    await _run_reconcile_case(drafter, case, reader)  # type: ignore[arg-type]
                    for case in cases
                ]
            else:
                outcomes = [
                    await _run_case(drafter, case, reader)  # type: ignore[arg-type]
                    for case in cases
                ]
            summaries.append(summarize(candidate.name, candidate.config.model, task, outcomes))

    return BaselineReport(
        generated_at=now if now is not None else datetime.now(UTC),
        task=task,
        adherence_floor=adherence_floor,
        summaries=summaries,
        recommended=_recommend(summaries, adherence_floor),
    )
