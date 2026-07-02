"""Scoring and aggregation for the baseline harness (SPEC-E10 §7)."""

from __future__ import annotations

from canonic.eval.models import CaseOutcome, StructuredOutcome
from canonic.eval.scoring import median, score_grain, summarize
from canonic.runtime.resolver import Task


def test_score_grain_is_order_insensitive() -> None:
    assert score_grain(["product_id", "order_id"], ["order_id", "product_id"])


def test_score_grain_rejects_missing_or_extra_column() -> None:
    assert not score_grain(["order_id"], ["order_id", "product_id"])
    assert not score_grain(["order_id", "product_id", "qty"], ["order_id", "product_id"])


def test_median_odd_and_even() -> None:
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5


def _outcome(
    *, correct: bool, structured: StructuredOutcome, latency: float, tokens: int | None
) -> CaseOutcome:
    return CaseOutcome(
        relation="r",
        correct=correct,
        structured=structured,
        latency_ms=latency,
        total_tokens=tokens,
    )


def test_summarize_computes_accuracy_adherence_and_p50() -> None:
    outcomes = [
        _outcome(correct=True, structured=StructuredOutcome.HONORED, latency=100.0, tokens=40),
        _outcome(correct=False, structured=StructuredOutcome.HONORED, latency=200.0, tokens=60),
        _outcome(
            correct=False, structured=StructuredOutcome.SCHEMA_INVALID, latency=300.0, tokens=None
        ),
    ]
    summary = summarize("small", "qwen2.5:3b", Task.DRAFT, outcomes)

    assert summary.n == 3
    assert summary.accuracy == 1 / 3  # one correct of three
    assert summary.schema_adherence == 2 / 3  # two honored of three
    assert summary.p50_latency_ms == 200.0
    assert summary.median_total_tokens == 50  # median of the two reported counts
    assert summary.outcome_counts[StructuredOutcome.HONORED] == 2
    assert summary.outcome_counts[StructuredOutcome.SCHEMA_INVALID] == 1
    assert summary.outcome_counts[StructuredOutcome.UNSUPPORTED] == 0


def test_summarize_tokens_none_when_unreported() -> None:
    outcomes = [
        _outcome(correct=True, structured=StructuredOutcome.HONORED, latency=10.0, tokens=None),
    ]
    assert summarize("m", "m", Task.DRAFT, outcomes).median_total_tokens is None
