"""Append-only event-log sink for canonic events (SPEC-E16 §2, §11 S4/S6)."""

from __future__ import annotations

import contextlib
import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, Union

from canonic.config import LOCAL_STATE_DIR, EventLogConfig

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl, rotation then runs unlocked
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import Iterator
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
    "event_log_paths",
    "expired_event_segments",
]

_EVENTS_FILE = "events.jsonl"
_LOCK_FILE = "events.lock"
_SEGMENT_PREFIX = "events-"
_SEGMENT_TIME_FORMAT = "%Y%m%dT%H%M%S%fZ"
#: The only high-volume kind. Everything else stays in the active file (see EventLogConfig).
_ROTATED_KIND = "served_answer"

CanonicEvent = Union["AnswerEvent", "ReconcileDecisionEvent", "FunnelEvent", "AnswerOutcomeEvent"]


class AnswerEventLog(Protocol):
    """Appends canonic events to the local event log (SPEC-E16 §2, S1-AC1, S4-AC1)."""

    def append(self, event: CanonicEvent) -> None:
        """Record one canonic event."""
        ...


def _segment_paths(state_dir: Path) -> list[Path]:
    """Rotated segments, oldest first (names embed the rotation time, so sorting is chronological)."""
    return sorted(state_dir.glob(f"{_SEGMENT_PREFIX}*.jsonl*"))


def event_log_paths(project_root: Path, *, include_segments: bool = True) -> list[Path]:
    """Event files to read, oldest first: rotated segments, then the active file.

    Segments hold ``served_answer`` events only, so readers of any other kind can pass
    ``include_segments=False`` and skip them.
    """
    state_dir = project_root / LOCAL_STATE_DIR
    paths = _segment_paths(state_dir) if include_segments else []
    active = state_dir / _EVENTS_FILE
    if active.exists():
        paths.append(active)
    return paths


def open_event_file(path: Path) -> Iterator[str]:
    """Iterate the lines of an active or (gzipped) segment event file."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        yield from handle


class DiskAnswerEventLog:
    """Appends ``.canonic/events.jsonl`` — one event per entry, local only.

    The directory is git-ignored (``LOCAL_STATE_DIR``) so the log never enters
    version control (SPEC-E16 §2). Appends and rotation are serialized with an advisory
    file lock because the MCP daemon and CLI commands write concurrently. When the active
    file reaches ``policy.max_bytes`` its ``served_answer`` events are moved into a segment
    and the retention limits are applied to the segments.
    """

    def __init__(self, project_root: Path, policy: EventLogConfig | None = None) -> None:
        self._dir = project_root / LOCAL_STATE_DIR
        self._path = self._dir / _EVENTS_FILE
        self._policy = policy if policy is not None else EventLogConfig()

    def append(self, event: CanonicEvent) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"
        with self._locked():
            if self._needs_rotation():
                self._rotate()
            with self._path.open("a") as f:
                f.write(line)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:  # pragma: no cover
            yield
            return
        with (self._dir / _LOCK_FILE).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _needs_rotation(self) -> bool:
        limit = self._policy.max_bytes
        if limit <= 0:
            return False
        try:
            return self._path.stat().st_size >= limit
        except FileNotFoundError:
            return False

    def _rotate(self) -> None:
        """Move ``served_answer`` events into a new segment, keep the rest active."""
        stamp = datetime.now(UTC)
        suffix = ".jsonl.gz" if self._policy.compress else ".jsonl"
        segment = self._dir / f"{_SEGMENT_PREFIX}{stamp.strftime(_SEGMENT_TIME_FORMAT)}{suffix}"
        segment_tmp = segment.with_name(segment.name + ".tmp")
        active_tmp = self._path.with_name(self._path.name + ".tmp")
        opener = gzip.open if self._policy.compress else open
        moved = 0
        with (
            self._path.open(encoding="utf-8") as source,
            opener(segment_tmp, "wt", encoding="utf-8") as seg_out,
            active_tmp.open("w", encoding="utf-8") as active_out,
        ):
            for raw in source:
                if _is_rotated_kind(raw):
                    seg_out.write(raw)
                    moved += 1
                else:
                    active_out.write(raw)
        if moved == 0:
            segment_tmp.unlink()
            active_tmp.unlink()
            return
        # Segment first: a crash in between leaves duplicates, never lost events.
        os.replace(segment_tmp, segment)
        os.replace(active_tmp, self._path)
        self._apply_retention(stamp)

    def _apply_retention(self, now: datetime) -> None:
        for path in expired_event_segments(self._dir.parent, self._policy, now):
            path.unlink(missing_ok=True)


def expired_event_segments(
    project_root: Path, policy: EventLogConfig, now: datetime | None = None
) -> list[Path]:
    """Segments that ``policy`` (``retention_days``, ``max_segments``) no longer keeps, oldest first."""
    segments = _segment_paths(project_root / LOCAL_STATE_DIR)
    expired: set[Path] = set()
    if policy.retention_days is not None:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=policy.retention_days)
        expired.update(p for p in segments if _segment_time(p) < cutoff)
    if policy.max_segments is not None:
        expired.update(segments[: max(0, len(segments) - policy.max_segments)])
    return [p for p in segments if p in expired]


def _is_rotated_kind(raw: str) -> bool:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("kind") == _ROTATED_KIND


def _segment_time(path: Path) -> datetime:
    """Rotation time of a segment. All its events are at most this old."""
    stem = path.name.removeprefix(_SEGMENT_PREFIX).split(".", 1)[0]
    try:
        return datetime.strptime(stem, _SEGMENT_TIME_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return datetime.max.replace(tzinfo=UTC)  # unknown name, never expire it


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
