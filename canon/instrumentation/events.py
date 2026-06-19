"""Append-only event-log sink for canon events (SPEC-E16 §2, §11 S4)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol, Union

from canon.config import LOCAL_STATE_DIR

if TYPE_CHECKING:
    from pathlib import Path

    from canon.instrumentation.models import AnswerEvent, ReconcileDecisionEvent

__all__ = ["AnswerEventLog", "CanonEvent", "DiskAnswerEventLog", "NullAnswerEventLog"]

_EVENTS_FILE = "events.jsonl"

CanonEvent = Union["AnswerEvent", "ReconcileDecisionEvent"]


class AnswerEventLog(Protocol):
    """Appends canon events to the local event log (SPEC-E16 §2, S1-AC1, S4-AC1)."""

    def append(self, event: CanonEvent) -> None:
        """Record one canon event."""
        ...


class DiskAnswerEventLog:
    """Appends ``.canon/events.jsonl`` — one event per entry, local only.

    The directory is git-ignored (``LOCAL_STATE_DIR``) so the log never enters
    version control (SPEC-E16 §2).
    """

    def __init__(self, project_root: Path) -> None:
        self._path = project_root / LOCAL_STATE_DIR / _EVENTS_FILE

    def append(self, event: CanonEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")


class NullAnswerEventLog:
    """No-op event log used when no project root is available."""

    def append(self, event: CanonEvent) -> None:
        pass
