"""The baseline harness: outcome classification, scoring, and recommendation (SPEC-E10 §7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from canon.eval.dataset import ReconcileCase
from canon.eval.harness import run_baseline
from canon.eval.models import StructuredOutcome
from canon.exc import (
    CredentialError,
    GenerationError,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canon.ingestion.reconciliation import ResolutionDraft
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


class StubReconcileDrafter:
    """A reconcile drafter that always picks a fixed winner index."""

    def __init__(self, *, winner: int | None = None, raises: Exception | None = None) -> None:
        self._winner = winner
        self._raises = raises

    async def draft_resolution(
        self,
        target: str,
        proposals: list,  # noqa: ANN001,ARG002
    ) -> ResolutionDraft | None:
        if self._raises is not None:
            raise self._raises
        if self._winner is None:
            return None
        return ResolutionDraft(winner_index=self._winner)


def _reconcile_factory(mapping: dict[str, StubReconcileDrafter]):
    def build(config):  # noqa: ANN001
        return mapping[config.model]

    return build


@pytest.fixture
def reconcile_cases() -> list[ReconcileCase]:
    return [
        ReconcileCase.model_validate(
            {
                "target": "semantics/w/orders.yaml",
                "proposals": [{"grain": ["order_id"]}, {"grain": ["id"]}],
                "expected_winner": 0,
            }
        ),
        ReconcileCase.model_validate(
            {
                "target": "semantics/w/users.yaml",
                "proposals": [{"grain": ["user_id"]}, {"grain": ["user_id", "tenant_id"]}],
                "expected_winner": 1,
            }
        ),
    ]


async def test_reconcile_baseline_correct_winner(reconcile_cases) -> None:
    drafter = StubReconcileDrafter(winner=0)
    candidate = make_candidate("strong", "llama3:70b")
    report = await run_baseline(
        [candidate],
        reconcile_cases,
        task=Task.RECONCILE,
        drafter_factory=_reconcile_factory({"llama3:70b": drafter}),
        usage_reader=StubUsageReader(tokens=100),
        now=datetime(2026, 6, 18, tzinfo=UTC),
    )

    (summary,) = report.summaries
    # Winner 0 is correct for case 0, wrong for case 1 (expected 1)
    assert summary.accuracy == 0.5
    assert summary.schema_adherence == 1.0
    assert report.task is Task.RECONCILE


async def test_reconcile_baseline_generation_error_maps_to_outcome(reconcile_cases) -> None:
    drafter = StubReconcileDrafter(raises=GenerationError("down"))
    candidate = make_candidate("m", "m")
    report = await run_baseline(
        [candidate],
        reconcile_cases,
        task=Task.RECONCILE,
        drafter_factory=_reconcile_factory({"m": drafter}),
        usage_reader=StubUsageReader(),
    )
    (summary,) = report.summaries
    assert summary.accuracy == 0.0
    assert all(o.structured is StructuredOutcome.ERROR for o in summary.outcomes)
    assert report.recommended is None
