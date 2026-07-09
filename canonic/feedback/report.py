"""Per-binding feedback-loop audit — the E11 learned-change trail (SPEC-E11 §6, S5-AC1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from canonic.config import FeedbackConfig
    from canonic.feedback.history import BindingOutcomeHistory

__all__ = ["BindingFeedbackEntry", "FeedbackReport", "build_feedback_report"]


class BindingFeedbackEntry(BaseModel):
    """One binding's outcome history and whether it crossed the pattern gate (SPEC-E11 §4-§6)."""

    model_config = ConfigDict(frozen=True)

    binding: str
    wrong_definition_count: int
    distinct_markers: int
    gated: bool
    """Whether this pattern crossed the gate (§4) — E4 contradiction evidence was emitted."""
    capped: bool
    """Whether the binding is currently capped at ``caution`` for serving (§5)."""
    refs: list[str]
    """The answer_outcome refs behind the wrong_definition pattern (§6 audit)."""


class FeedbackReport(BaseModel):
    """The feedback-loop audit surfaced by ``canonic report`` (SPEC-E11 §6, S5-AC1)."""

    model_config = ConfigDict(frozen=True)

    entries: list[BindingFeedbackEntry] = []


def build_feedback_report(history: BindingOutcomeHistory, config: FeedbackConfig) -> FeedbackReport:
    """Build the per-binding feedback audit for every binding with wrong_definition history.

    Every binding is reported here regardless of whether it crossed the pattern gate — a
    single incident is visible in this audit even though it never reaches E4 (S2-AC1) — so a
    human reviewing ``canonic report`` can see the pattern building before it fires.
    """
    entries: list[BindingFeedbackEntry] = []
    for binding in history.bindings():
        count = history.wrong_definition_count(binding, window_days=config.pattern_window_days)
        if count == 0:
            continue
        markers = history.distinct_markers(binding, window_days=config.pattern_window_days)
        entries.append(
            BindingFeedbackEntry(
                binding=binding,
                wrong_definition_count=count,
                distinct_markers=markers,
                gated=count >= config.pattern_min_count and markers >= config.pattern_min_markers,
                capped=history.is_capped(binding, window_days=config.trust_cap_window_days),
                refs=history.wrong_definition_refs(binding, window_days=config.pattern_window_days),
            )
        )
    return FeedbackReport(
        entries=sorted(entries, key=lambda e: (-e.wrong_definition_count, e.binding))
    )
