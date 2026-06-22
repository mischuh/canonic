"""E10 tested local-model baseline — the accuracy harness for ``draft`` and ``reconcile`` tasks.

A focused, E10-owned harness that measures the LLM-in-loop tasks that feed compilable semantics:
``draft`` (grain inference) and ``reconcile`` (contradiction resolution). Not literal compiler
quality — the E5 compiler is deterministic and LLM-free. Drives the real production drafter paths
over candidate local models and labeled sets, scoring accuracy, structured (JSON-schema) output
behavior, and latency, then renders a per-release baseline doc. The broad query-accuracy E16
harness (PRD FR-14) is a separate Phase-2 concern; this is only the slice §7 needs.
"""

from __future__ import annotations

from canon.eval.harness import run_baseline
from canon.eval.models import BaselineReport, CaseOutcome, ModelTaskSummary, StructuredOutcome

__all__ = [
    "BaselineReport",
    "CaseOutcome",
    "ModelTaskSummary",
    "StructuredOutcome",
    "run_baseline",
]
