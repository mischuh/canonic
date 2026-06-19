"""The baseline harness: run candidate models through the real ``draft`` path (SPEC-E10 §7).

``run_baseline`` drives :class:`~canon.runtime.drafter.RuntimeLLMDrafter` (the production grain
drafter) over each candidate and the labeled set, classifying every run into a
:class:`~canon.eval.models.StructuredOutcome` and scoring its grain. Reusing the real drafter —
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

from canon.eval.models import BaselineReport, CaseOutcome, ModelTaskSummary, StructuredOutcome
from canon.eval.scoring import score_grain, summarize
from canon.exc import (
    CredentialError,
    GenerationError,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canon.runtime.resolver import Task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from canon.config import LLMConfig
    from canon.connectors.base import RelationSchema
    from canon.eval.candidates import NamedCandidate
    from canon.eval.dataset import GrainCase
    from canon.ingestion.builder import GrainDraft

__all__ = ["GrainDrafter", "LiteLLMUsageReader", "UsageReader", "run_baseline"]

#: Default share of cases a candidate must HONOR (schema-valid output) to be recommendable.
DEFAULT_ADHERENCE_FLOOR = 0.9


class GrainDrafter(Protocol):
    """The production grain-drafting seam the harness measures (RuntimeLLMDrafter satisfies it)."""

    async def draft_grain(self, schema: RelationSchema) -> GrainDraft: ...


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
    from canon.runtime.drafter import RuntimeLLMDrafter
    from canon.runtime.generation import GenerationRuntime

    return RuntimeLLMDrafter(GenerationRuntime(config))


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
    cases: Sequence[GrainCase],
    *,
    task: Task = Task.DRAFT,
    drafter_factory: Callable[[LLMConfig], GrainDrafter] = _default_drafter_factory,
    usage_reader: UsageReader | None = None,
    adherence_floor: float = DEFAULT_ADHERENCE_FLOOR,
    now: datetime | None = None,
) -> BaselineReport:
    """Run every candidate over the labeled set and build the per-task baseline report.

    Args:
        candidates: Models to evaluate (a friendly name + resolved ``LLMConfig`` each).
        cases: The labeled grain set.
        task: The task this baseline covers (``draft`` in v1; ``reconcile`` is pending an E4
            call site, so the harness is exercised only for ``draft``).
        drafter_factory: Builds the drafter for a candidate; defaults to the real
            generation-backed :class:`RuntimeLLMDrafter`. Injected so tests run without a network.
        usage_reader: Token probe; defaults to :class:`LiteLLMUsageReader`. Injected for tests.
        adherence_floor: Minimum structured-output adherence to be recommendable.
        now: Override for ``generated_at`` (tests); defaults to current UTC.
    """
    reader = usage_reader if usage_reader is not None else LiteLLMUsageReader()
    summaries: list[ModelTaskSummary] = []
    with reader:
        for candidate in candidates:
            drafter = drafter_factory(candidate.config)
            outcomes = [await _run_case(drafter, case, reader) for case in cases]
            summaries.append(summarize(candidate.name, candidate.config.model, task, outcomes))

    return BaselineReport(
        generated_at=now if now is not None else datetime.now(UTC),
        task=task,
        adherence_floor=adherence_floor,
        summaries=summaries,
        recommended=_recommend(summaries, adherence_floor),
    )
