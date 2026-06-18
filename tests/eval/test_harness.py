"""The baseline harness: outcome classification, scoring, and recommendation (SPEC-E10 §7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from canon.eval.harness import run_baseline
from canon.eval.models import StructuredOutcome
from canon.exc import (
    CredentialError,
    GenerationError,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canon.runtime.resolver import Task
from tests.eval.conftest import StubDrafter, StubUsageReader, make_candidate


def _factory(mapping: dict[str, StubDrafter]):
    """A drafter factory that dispatches on the candidate's model id."""

    def build(config):  # noqa: ANN001 — LLMConfig, kept local to the test
        return mapping[config.model]

    return build


async def test_honored_run_scores_grain_and_records_usage(grain_cases) -> None:
    # Drafter always answers "id" — correct for app.orders, wrong for the composite case.
    drafter = StubDrafter(grain=["id"])
    candidate = make_candidate("small", "qwen2.5:3b")

    report = await run_baseline(
        [candidate],
        grain_cases,
        drafter_factory=_factory({"qwen2.5:3b": drafter}),
        usage_reader=StubUsageReader(tokens=42),
        now=datetime(2026, 6, 18, tzinfo=UTC),
    )

    (summary,) = report.summaries
    assert summary.accuracy == 0.5  # one of two cases matched
    assert summary.schema_adherence == 1.0  # both honored the schema
    assert all(o.structured is StructuredOutcome.HONORED for o in summary.outcomes)
    assert summary.median_total_tokens == 42
    assert report.generated_at == datetime(2026, 6, 18, tzinfo=UTC)
    assert report.task is Task.DRAFT


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (StructuredOutputUnsupported("no schema support"), StructuredOutcome.UNSUPPORTED),
        (StructuredOutputError("bad json"), StructuredOutcome.SCHEMA_INVALID),
        (GenerationError("provider down"), StructuredOutcome.ERROR),
        (CredentialError("missing key"), StructuredOutcome.ERROR),
    ],
)
async def test_each_generation_error_maps_to_outcome(grain_cases, exc, expected) -> None:
    candidate = make_candidate("flaky", "m")
    report = await run_baseline(
        [candidate],
        grain_cases,
        drafter_factory=_factory({"m": StubDrafter(raises=exc)}),
        usage_reader=StubUsageReader(),
    )

    (summary,) = report.summaries
    assert summary.accuracy == 0.0
    assert summary.schema_adherence == 0.0
    assert all(o.structured is expected for o in summary.outcomes)
    assert all(o.correct is False for o in summary.outcomes)
    assert all(o.error == str(exc) for o in summary.outcomes)
    # Even a failure is timed.
    assert all(o.latency_ms >= 0.0 for o in summary.outcomes)
    assert report.recommended is None  # nothing clears the adherence floor


async def test_recommendation_gates_on_adherence_then_accuracy(grain_cases) -> None:
    # accurate_but_broken: perfect grain knowledge but never honors the schema -> unusable.
    # solid: honors the schema and gets the surrogate-key case right.
    factory = _factory(
        {
            "broken": StubDrafter(raises=StructuredOutputError("nope")),
            "solid": StubDrafter(grain=["id"]),
        }
    )
    report = await run_baseline(
        [make_candidate("accurate-but-broken", "broken"), make_candidate("solid", "solid")],
        grain_cases,
        drafter_factory=factory,
        usage_reader=StubUsageReader(),
        adherence_floor=0.9,
    )

    assert report.recommended == "solid"


async def test_no_recommendation_when_none_clear_floor(grain_cases) -> None:
    report = await run_baseline(
        [make_candidate("a", "a")],
        grain_cases,
        drafter_factory=_factory({"a": StubDrafter(raises=GenerationError("x"))}),
        usage_reader=StubUsageReader(),
    )
    assert report.recommended is None
