"""Scoring and aggregation for the baseline harness (SPEC-E10 §7, GH-66)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canon.eval.models import CaseOutcome, ModelTaskSummary, StructuredOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canon.runtime.resolver import Task

__all__ = ["median", "score_grain", "summarize"]


def score_grain(predicted: Sequence[str], expected: Sequence[str]) -> bool:
    """A grain is correct when its column set matches exactly, order-insensitively.

    Grain is a *set* of columns (the minimal unique key), so ``[a, b]`` and ``[b, a]`` are the
    same answer; a missing or extra column is wrong.
    """
    return set(predicted) == set(expected)


def median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence (the p50 used for latency/tokens)."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize(
    name: str, model: str, task: Task, outcomes: Sequence[CaseOutcome]
) -> ModelTaskSummary:
    """Aggregate a candidate's per-case outcomes into a task summary.

    Accuracy and structured-output adherence are both over the full case count, so a model that
    errors or returns unparseable output is penalized on accuracy as well as adherence.
    """
    n = len(outcomes)
    counts: dict[StructuredOutcome, int] = {o: 0 for o in StructuredOutcome}
    for outcome in outcomes:
        counts[outcome.structured] += 1

    correct = sum(1 for o in outcomes if o.correct)
    honored = counts[StructuredOutcome.HONORED]
    latencies = [o.latency_ms for o in outcomes] or [0.0]
    token_values = [o.total_tokens for o in outcomes if o.total_tokens is not None]

    return ModelTaskSummary(
        name=name,
        model=model,
        task=task,
        n=n,
        accuracy=correct / n if n else 0.0,
        schema_adherence=honored / n if n else 0.0,
        p50_latency_ms=median(latencies),
        median_total_tokens=int(median(token_values)) if token_values else None,
        outcome_counts=counts,
        outcomes=list(outcomes),
    )
