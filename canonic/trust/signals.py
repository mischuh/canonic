"""Pluggable trust signals — each a pure function from raw inputs to a SignalVerdict.

``provenance_signal`` is purely static (available from E14 v1 onward). Two signals read
persisted, per-binding history and are inactive until a caller supplies one (SPEC-E14 §4,
"when a source isn't online yet ... that signal is simply inactive"):

- ``assertion_signal`` reads the E16 accuracy harness's persisted pass/fail snapshot
  (:class:`canonic.feedback.assertion_history.AssertionHistory`, written by
  ``canonic assert`` — SPEC-E14 §5, "+ E16 Phase 2"): a benchmarked, passing metric can
  reach ``trusted``; a failing one is capped at ``caution``.
- ``outcome_signal`` reads E11's per-binding outcome history
  (:class:`canonic.feedback.history.BindingOutcomeHistory`, SPEC-E11 §5).

Drift and contradiction are currently build-time/knowledge-page signals, not persisted
per binding, so there is nothing to read at serve time yet; add them here once their
source lands — the worst-signal-dominates scorer needs no other change (SPEC-E14 §5 —
"no schema break at any step").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.semantic.models import Provenance
from canonic.trust.models import SignalVerdict, TrustTier

if TYPE_CHECKING:
    from canonic.compiler.result import SourceFreshness, TrustInput
    from canonic.feedback.assertion_history import AssertionHistory
    from canonic.feedback.history import BindingOutcomeHistory

__all__ = [
    "assertion_signal",
    "finality_signal",
    "freshness_signal",
    "outcome_signal",
    "provenance_signal",
    "static_signals_for",
]


def provenance_signal(trust_input: TrustInput) -> SignalVerdict:
    """Binding provenance (SPEC-E14 §3 table row "Binding provenance")."""
    if trust_input.provenance == Provenance.INFERRED.value:
        return SignalVerdict(
            cap=TrustTier.PROVISIONAL, reason=f"{trust_input.metric}: binding inferred"
        )
    return SignalVerdict(cap=None)


def assertion_signal(
    trust_input: TrustInput, history: AssertionHistory | None = None
) -> SignalVerdict:
    """Assertion coverage/validation (SPEC-E14 §3 table row "Assertion").

    An authored assertion is necessary but not sufficient for ``trusted`` on its own —
    it must also have a persisted, passing verdict from the E16 accuracy harness
    (``canonic assert``, SPEC-E14 §5 "+ E16 Phase 2"). Without ``history`` (or when the
    binding has no recorded verdict — never harnessed, or dropped from the current
    assertion set), every metric caps at ``provisional``, exactly as before E16 Phase 2.
    A recorded *failing* verdict caps at ``caution`` (SPEC-E14 §7 AC1); a recorded
    *passing* verdict is inactive (``cap=None``), letting the tier rise to ``trusted``
    if nothing else caps it.
    """
    if not trust_input.has_assertion:
        return SignalVerdict(
            cap=TrustTier.PROVISIONAL, reason=f"{trust_input.metric}: untested (no assertion)"
        )
    record = history.status_for(trust_input.binding) if history and trust_input.binding else None
    if record is None:
        return SignalVerdict(
            cap=TrustTier.PROVISIONAL,
            reason=f"{trust_input.metric}: assertion unverified (pass/fail not yet persisted)",
        )
    if not record.passed:
        return SignalVerdict(
            cap=TrustTier.CAUTION,
            reason=f"{trust_input.metric}: assertion failed ({record.assertion_id})",
        )
    return SignalVerdict(cap=None)


def static_signals_for(
    trust_inputs: list[TrustInput], assertion_history: AssertionHistory | None = None
) -> list[SignalVerdict]:
    """The compile-time-available signal set for a set of queried metrics.

    ``assertion_history`` is optional (default inactive, matching pre-E16-Phase-2
    behavior) — see :func:`assertion_signal`.
    """
    signals: list[SignalVerdict] = []
    for trust_input in trust_inputs:
        signals.append(provenance_signal(trust_input))
        signals.append(assertion_signal(trust_input, assertion_history))
    return signals


def finality_signal(final_rows: int | None, provisional_rows: int | None) -> SignalVerdict:
    """Finality of the served rows (SPEC-E14 §3 table row "Finality"). Serve-time only —
    row-level final/provisional counts are not known until the query has executed.
    """
    if provisional_rows:
        return SignalVerdict(
            cap=TrustTier.PROVISIONAL, reason="finality: provisional rows included"
        )
    return SignalVerdict(cap=None)


def freshness_signal(freshness: list[SourceFreshness]) -> SignalVerdict:
    """Source freshness (SPEC-E14 §3 table row "Freshness").

    ``stale`` is always ``False`` in P0 (no staleness policy defined yet — SPEC-E5-E15),
    so this signal is inactive today; it activates automatically once P0 gains one.
    """
    stale = sorted(f.source for f in freshness if f.stale)
    if stale:
        return SignalVerdict(
            cap=TrustTier.PROVISIONAL, reason=f"freshness: stale ({', '.join(stale)})"
        )
    return SignalVerdict(cap=None)


def outcome_signal(
    trust_input: TrustInput, history: BindingOutcomeHistory, window_days: int
) -> SignalVerdict:
    """A recent confirmed-``wrong_definition`` outcome caps a binding at ``caution`` (SPEC-E11 §5).

    Only ``wrong_definition`` outcomes ever cap trust — the attribution safeguard (SPEC-E11
    §3) is enforced inside :meth:`~canonic.feedback.history.BindingOutcomeHistory.is_capped`,
    so ``wrong_data``/``wrong_interpretation``/``unspecified`` never reach this signal.
    Inactive when the metric has no derivable binding key (e.g. recompute_at_grain kinds)
    or no capping history.
    """
    if trust_input.binding is None:
        return SignalVerdict(cap=None)
    if history.is_capped(trust_input.binding, window_days=window_days):
        return SignalVerdict(cap=TrustTier.CAUTION, reason="outcome: confirmed-wrong")
    return SignalVerdict(cap=None)
