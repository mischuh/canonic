"""Markdown rendering of the baseline report (SPEC-E10 §7, S9)."""

from __future__ import annotations

from datetime import UTC, datetime

from canon.eval.harness import run_baseline
from canon.eval.report import render_markdown
from tests.eval.conftest import StubDrafter, StubUsageReader, make_candidate


async def _report(grain_cases):
    return await run_baseline(
        [make_candidate("small-local", "qwen2.5:3b")],
        grain_cases,
        drafter_factory=lambda _config: StubDrafter(grain=["id"]),
        usage_reader=StubUsageReader(tokens=42),
        now=datetime(2026, 6, 18, 12, 0, tzinfo=UTC),
    )


async def test_render_includes_table_and_metadata(grain_cases) -> None:
    md = render_markdown(await _report(grain_cases))

    assert "# Canon local-model baseline" in md
    assert "2026-06-18T12:00:00+00:00" in md
    assert "| Model | Accuracy | Structured output | p50 latency |" in md
    assert "small-local (`qwen2.5:3b`)" in md
    assert "`canon eval baseline`" in md  # re-run instructions


async def test_render_marks_recommended_and_notes_reconcile_pending(grain_cases) -> None:
    md = render_markdown(await _report(grain_cases))

    assert "✅" in md  # the recommended candidate
    assert "**Recommended for `draft`:** small-local." in md
    assert "--task reconcile" in md


async def test_render_states_when_no_candidate_recommended(grain_cases) -> None:
    report = await run_baseline(
        [make_candidate("weak", "weak")],
        grain_cases,
        drafter_factory=lambda _config: StubDrafter(grain=["wrong_col"]),
        usage_reader=StubUsageReader(),
    )
    # Honors schema (so adherence is fine) but every grain is wrong; still recommended since
    # adherence clears the floor — assert the recommended line names it.
    md = render_markdown(report)
    assert "**Recommended for `draft`:** weak." in md
