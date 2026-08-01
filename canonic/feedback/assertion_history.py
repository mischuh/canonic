"""Per-binding assertion pass/fail history — E16 Phase 2's assertion signal (SPEC-E14 §5).

A JSON snapshot at ``.canonic/assertions.json``, overwritten on every ``canonic assert``
run (SPEC-E14 §5, "+ E16 Phase 2 — assertion/accuracy signal activates: a benchmarked,
passing metric can reach `trusted`; a failing one is capped at `caution`"). Unlike E11's
outcome history (:mod:`canonic.feedback.history`), this is not an append-only event
join — trust scoring only needs "what did the *last* harness run say about this
binding," so a binding no longer covered by the current assertion set reverts to
unverified rather than trusting stale data indefinitely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import TYPE_CHECKING

from canonic.config import LOCAL_STATE_DIR

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.contracts.assertions import AssertionOutcome

__all__ = ["AssertionHistory", "AssertionRecord", "write_assertion_results"]

_ASSERTIONS_FILE = "assertions.json"


@dataclass(frozen=True, slots=True)
class AssertionRecord:
    """The latest harness verdict recorded against one ``"source.measure"`` binding."""

    ts: str
    assertion_id: str
    passed: bool


class AssertionHistory:
    """Per-binding latest assertion verdict, joined from ``.canonic/assertions.json``.

    Keyed by the same resolved ``"source.measure"`` binding string as
    :class:`canonic.feedback.history.BindingOutcomeHistory` and
    :attr:`canonic.compiler.result.TrustInput.binding` — the E14 trust-scoring join key.
    """

    def __init__(self, records: dict[str, AssertionRecord]) -> None:
        self._by_binding = dict(records)

    @classmethod
    def from_project(cls, root: Path) -> AssertionHistory:
        """Build history from ``.canonic/assertions.json`` (empty when absent or unreadable)."""
        path = root / LOCAL_STATE_DIR / _ASSERTIONS_FILE
        try:
            raw = json.loads(path.read_text())
        except (OSError, JSONDecodeError):
            return cls({})
        if not isinstance(raw, dict):
            return cls({})
        records: dict[str, AssertionRecord] = {}
        for binding, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                records[binding] = AssertionRecord(
                    ts=str(entry["ts"]),
                    assertion_id=str(entry["assertion_id"]),
                    passed=bool(entry["passed"]),
                )
            except KeyError:
                continue
        return cls(records)

    def status_for(self, binding: str) -> AssertionRecord | None:
        """The latest recorded verdict for ``binding``, or ``None`` if never harnessed."""
        return self._by_binding.get(binding)


def write_assertion_results(root: Path, outcomes: list[AssertionOutcome]) -> None:
    """Persist one harness run's per-binding verdicts, overwriting the previous snapshot.

    When two outcomes in the same run cover the same binding, the merge is worst-wins
    (``passed`` is ``True`` only if every covering assertion passed) — consistent with
    the trust scorer's own worst-signal-dominates rule (SPEC-E14 §3). The failing
    assertion's id is kept as the record's ``assertion_id`` when there is one, so the
    surfaced reason points at the actual failure.
    """
    now = datetime.now(UTC).isoformat()
    per_binding: dict[str, AssertionRecord] = {}
    for outcome in outcomes:
        for binding in outcome.bindings:
            existing = per_binding.get(binding)
            if existing is None:
                per_binding[binding] = AssertionRecord(
                    ts=now, assertion_id=outcome.assertion_id, passed=outcome.passed
                )
                continue
            if not outcome.passed and existing.passed:
                per_binding[binding] = AssertionRecord(
                    ts=now, assertion_id=outcome.assertion_id, passed=False
                )
            elif not outcome.passed and not existing.passed:
                per_binding[binding] = AssertionRecord(
                    ts=now, assertion_id=existing.assertion_id, passed=False
                )
            # else: outcome passed and existing already recorded — keep existing.

    path = root / LOCAL_STATE_DIR / _ASSERTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        binding: {"ts": rec.ts, "assertion_id": rec.assertion_id, "passed": rec.passed}
        for binding, rec in per_binding.items()
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2))
