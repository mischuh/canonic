"""Per-binding outcome history — the E11 aggregation of E16 answer_outcome events (SPEC-E11 §3-§5).

Only ``wrong_definition`` outcomes may ever implicate a binding (SPEC-E11 §3, the attribution
safeguard): a wrong answer can be a data-quality problem or a misread question, neither of
which canon should "learn" from. That filter is enforced once here, at aggregation, so neither
the pattern gate (:func:`canonic.feedback.evidence.outcome_evidence`, §4) nor the trust cap
(:func:`canonic.trust.signals.outcome_signal`, §5) can accidentally act on a quarantined
reason_code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from canonic.instrumentation.models import OutcomeReasonCode, OutcomeVerdict
from canonic.instrumentation.report import latest_outcome_by_ref, read_events

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.instrumentation.models import (
        AnswerEvent,
        AnswerOutcomeEvent,
        OutcomeMarkedBy,
    )

__all__ = ["BindingOutcomeHistory", "OutcomeRecord"]


def _parse_ts(ts: str) -> datetime:
    """Parse an event's ISO timestamp, defaulting a naive value to UTC."""
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One outcome mark joined to the ``source.measure`` binding it was served against."""

    ts: str
    binding: str
    verdict: OutcomeVerdict
    reason_code: OutcomeReasonCode | None
    marked_by: OutcomeMarkedBy
    ref: str


class BindingOutcomeHistory:
    """Per-binding outcome history, joined from the local event log (SPEC-E11 §3-§6).

    Reuses the same ``ref == query_hash`` join and per-``ref`` dedup as
    :func:`canonic.instrumentation.report.build_correction_recurrence` (via
    :func:`~canonic.instrumentation.report.latest_outcome_by_ref`) so the two never drift on
    what counts as one outcome. Keyed by the resolved ``"source.measure"`` binding string —
    the same value :attr:`canonic.instrumentation.models.AnswerEvent.resolved` records — not
    by metric name, so aliases of the same physical binding share one history.
    """

    def __init__(self, records: list[OutcomeRecord]) -> None:
        self._by_binding: dict[str, list[OutcomeRecord]] = {}
        for record in records:
            self._by_binding.setdefault(record.binding, []).append(record)

    @classmethod
    def from_project(cls, root: Path) -> BindingOutcomeHistory:
        """Build history from ``.canonic/events.jsonl`` (empty when nothing is recorded yet)."""
        answers = read_events(root, kind="served_answer")
        outcomes = read_events(root, kind="answer_outcome")
        return cls.from_events(answers, outcomes)

    @classmethod
    def from_events(
        cls, answers: list[AnswerEvent], outcomes: list[AnswerOutcomeEvent]
    ) -> BindingOutcomeHistory:
        by_hash = {a.query_hash: a for a in answers}
        records: list[OutcomeRecord] = []
        for ref, outcome in latest_outcome_by_ref(outcomes).items():
            answer = by_hash.get(ref)
            if answer is None:
                continue
            for binding in answer.resolved.get("metrics", {}).values():
                records.append(
                    OutcomeRecord(
                        ts=outcome.ts,
                        binding=binding,
                        verdict=outcome.verdict,
                        reason_code=outcome.reason_code,
                        marked_by=outcome.marked_by,
                        ref=ref,
                    )
                )
        return cls(records)

    def bindings(self) -> list[str]:
        """Every binding with at least one recorded outcome, sorted for determinism."""
        return sorted(self._by_binding)

    def records_for(self, binding: str) -> list[OutcomeRecord]:
        """Every outcome record on ``binding``, in event order (§6 audit)."""
        return list(self._by_binding.get(binding, ()))

    def _wrong_definition_records(
        self, binding: str, *, window_days: int | None = None
    ) -> list[OutcomeRecord]:
        records = [
            r
            for r in self._by_binding.get(binding, ())
            if r.reason_code is OutcomeReasonCode.WRONG_DEFINITION
        ]
        if window_days is None:
            return records
        cutoff = datetime.now(UTC) - timedelta(days=window_days)
        return [r for r in records if _parse_ts(r.ts) >= cutoff]

    def wrong_definition_count(self, binding: str, *, window_days: int) -> int:
        """Count of ``wrong_definition`` outcomes on ``binding`` within ``window_days`` (§4)."""
        return len(self._wrong_definition_records(binding, window_days=window_days))

    def distinct_markers(self, binding: str, *, window_days: int) -> int:
        """Distinct ``marked_by`` roles among recent ``wrong_definition`` outcomes (§4/§9)."""
        records = self._wrong_definition_records(binding, window_days=window_days)
        return len({r.marked_by for r in records})

    def wrong_definition_refs(self, binding: str, *, window_days: int) -> list[str]:
        """Sorted, deduped ``answer_outcome`` refs behind the recent ``wrong_definition``
        pattern on ``binding`` — the FR-9 audit anchor (§6) and E4 evidence anchor (§4).
        """
        records = self._wrong_definition_records(binding, window_days=window_days)
        return sorted({r.ref for r in records})

    def is_capped(self, binding: str, *, window_days: int) -> bool:
        """Whether ``binding`` should be capped at ``caution`` right now (§5).

        True when the latest ``wrong_definition`` outcome falls inside ``window_days`` and no
        later ``correct`` outcome on the same binding has been recorded since — an accepted
        fix lifts the cap before the window would otherwise expire (§5, §9 decay).
        """
        wrong = self._wrong_definition_records(binding)
        if not wrong:
            return False
        latest_wrong = max(wrong, key=lambda r: r.ts)
        if _parse_ts(latest_wrong.ts) < datetime.now(UTC) - timedelta(days=window_days):
            return False
        later_correct = any(
            r.verdict is OutcomeVerdict.CORRECT and r.ts > latest_wrong.ts
            for r in self._by_binding.get(binding, ())
        )
        return not later_correct
