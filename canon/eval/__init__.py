"""E10 tested local-model baseline — the draft accuracy harness (SPEC-E10 §7, GH-66).

A focused, E10-owned harness that measures the LLM-in-loop *drafting* that feeds compilable
semantics — **not** literal compiler quality, since the E5 compiler is deterministic and
LLM-free. It drives the real ``draft`` (grain inference) path over candidate local models and a
labeled set, scoring accuracy, structured (JSON-schema) output behavior, and latency, then renders
a per-release baseline doc. The broad query-accuracy E16 harness (PRD FR-14) is a separate Phase-2
concern; this is only the slice §7 needs.

``reconcile`` has no live E4 call site yet, so the v1 baseline is ``draft``-only; the harness is
built generic so ``reconcile`` slots in once E4 reconciliation drafting exists.
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
