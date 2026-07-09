"""E11 evidence minting — outcome history → EvidenceItem for E4 reconciliation (SPEC-E11 §4).

Pattern-gated, not single-incident (S2): a binding only produces evidence once it has crossed
``config.pattern_min_count`` distinct-enough ``wrong_definition`` outcomes within the
configured window. A single outcome is still visible in :mod:`canonic.feedback.report` (the
§6 audit) but never reaches E4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from canonic.connectors.base import AcquisitionTier
from canonic.contracts.loader import load_metric_bindings
from canonic.contracts.models import BindingKind, Status
from canonic.ingestion.models import EvidenceItem, EvidenceKind
from canonic.instrumentation.models import _sha256_json

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.config import FeedbackConfig
    from canonic.feedback.history import BindingOutcomeHistory

__all__ = ["outcome_evidence"]


def _metric_for_binding(root: Path, binding: str) -> str | None:
    """Resolve a ``source.measure`` binding string to its active contract metric name.

    Only single-source-bound kinds (single/semi_additive/distinct_count/percentile/opaque)
    carry a source+column pair to match against; composite bindings (ratio/weighted_avg) have
    no physical binding of their own. Returns ``None`` when no active binding matches — E11
    never fabricates a target for a metric that no longer exists (§4: it flags an existing
    binding, it never invents one).
    """
    if "." not in binding:
        return None
    source, _, measure = binding.partition(".")
    for mb in load_metric_bindings(root):
        if mb.status is not Status.ACTIVE:
            continue
        ref = mb.canonical
        if ref.source != source:
            continue
        candidate: str | None
        if ref.kind in {BindingKind.SINGLE, BindingKind.SEMI_ADDITIVE, BindingKind.OPAQUE}:
            candidate = ref.measure
        elif ref.kind is BindingKind.DISTINCT_COUNT:
            candidate = ref.distinct_on
        elif ref.kind is BindingKind.PERCENTILE:
            candidate = ref.column
        else:
            continue
        if candidate == measure:
            return mb.metric
    return None


def outcome_evidence(
    root: Path, history: BindingOutcomeHistory, config: FeedbackConfig
) -> list[EvidenceItem]:
    """Mint contradiction evidence for bindings whose wrong_definition pattern crossed the gate.

    A binding is gated when it has at least ``config.pattern_min_count`` ``wrong_definition``
    outcomes from at least ``config.pattern_min_markers`` distinct markers within
    ``config.pattern_window_days`` (SPEC-E11 §4, S2-AC2). Every other reason_code is
    quarantined by construction — :class:`~canonic.feedback.history.BindingOutcomeHistory`
    only ever counts ``wrong_definition`` toward this gate (§3, S1).
    """
    items: list[EvidenceItem] = []
    for binding in history.bindings():
        count = history.wrong_definition_count(binding, window_days=config.pattern_window_days)
        if count < config.pattern_min_count:
            continue
        markers = history.distinct_markers(binding, window_days=config.pattern_window_days)
        if markers < config.pattern_min_markers:
            continue
        metric = _metric_for_binding(root, binding)
        if metric is None:
            continue  # no active binding to implicate — nothing to flag (§4)

        refs = history.wrong_definition_refs(binding, window_days=config.pattern_window_days)
        payload = {
            "metric": metric,
            "binding": binding,
            "count": count,
            "window_days": config.pattern_window_days,
            "distinct_markers": markers,
            "refs": refs,
        }
        items.append(
            EvidenceItem(
                source="canonic.feedback",
                kind=EvidenceKind.ANSWER_OUTCOME.value,
                acquisition_tier=AcquisitionTier.QUERY_HISTORY,
                payload=payload,
                source_fingerprint=_sha256_json({"metric": metric, "refs": refs}),
                observed_at=datetime.now(UTC),
            )
        )
    return items
