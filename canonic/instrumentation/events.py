"""Append-only event-log sink for canonic events (SPEC-E16 §2, §11 S4/S6)."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, Union

from canonic.config import LOCAL_STATE_DIR

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.instrumentation.models import (
        AnswerEvent,
        AnswerOutcomeEvent,
        FunnelEvent,
        FunnelMilestone,
        ReconcileDecisionEvent,
    )

__all__ = [
    "AnswerEventLog",
    "CanonicEvent",
    "DiskAnswerEventLog",
    "NullAnswerEventLog",
    "emit_milestone",
    "emit_milestone_once",
]

_EVENTS_FILE = "events.jsonl"

CanonicEvent = Union["AnswerEvent", "ReconcileDecisionEvent", "FunnelEvent", "AnswerOutcomeEvent"]


class AnswerEventLog(Protocol):
    """Appends canonic events to the local event log (SPEC-E16 §2, S1-AC1, S4-AC1)."""

    def append(self, event: CanonicEvent) -> None:
        """Record one canonic event."""
        ...


class DiskAnswerEventLog:
    """Appends ``.canonic/events.jsonl`` — one event per entry, local only.

    The directory is git-ignored (``LOCAL_STATE_DIR``) so the log never enters
    version control (SPEC-E16 §2).
    """

    def __init__(self, project_root: Path) -> None:
        self._path = project_root / LOCAL_STATE_DIR / _EVENTS_FILE

    def append(self, event: CanonicEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")


class NullAnswerEventLog:
    """No-op event log used when no project root is available."""

    def append(self, event: CanonicEvent) -> None:
        pass


def emit_milestone(log: AnswerEventLog, milestone: FunnelMilestone) -> None:
    """Append a FunnelEvent for ``milestone``; errors are swallowed so logging never aborts callers."""
    from canonic.instrumentation.models import FunnelEvent

    with contextlib.suppress(Exception):
        log.append(FunnelEvent(ts=datetime.now(UTC).isoformat(), milestone=milestone))


def emit_milestone_once(root: Path, milestone: FunnelMilestone) -> None:
    """Append ``milestone`` only if it has not been recorded yet (idempotent guard)."""
    from canonic.instrumentation.report import read_events

    existing = read_events(root, kind="funnel_milestone")
    if any(e.milestone == milestone for e in existing):
        return
    emit_milestone(DiskAnswerEventLog(root), milestone)
