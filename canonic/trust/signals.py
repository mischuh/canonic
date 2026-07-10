"""Pluggable trust signals — each a pure function from raw inputs to a SignalVerdict.

Static signals (provenance, assertion coverage) are available from E14 v1 onward.
``outcome_signal`` is the one dynamic signal: E11's per-binding outcome history
(:class:`canonic.feedback.history.BindingOutcomeHistory`), folded in only when a caller
supplies one (SPEC-E11 §5). Signals not yet backed by real data are deliberately omitted
here rather than faked, per SPEC-E14 §4 ("when a source isn't online yet ... that signal is
simply inactive"):

- Assertion *pass/fail* needs a per-binding result from the E16 accuracy harness; the
  harness itself exists, but its outcomes are not yet persisted to a store this signal
  could join against at serve time.
- Drift and contradiction are currently build-time/knowledge-page signals, not persisted
  per binding, so there is nothing to read at serve time.

Add them here once their source lands; the worst-signal-dominates scorer needs no other
change (SPEC-E14 §5 — "no schema break at any step").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.semantic.models import Provenance
from canonic.trust.models import SignalVerdict, TrustTier

if TYPE_CHECKING:
    from canonic.compiler.result import SourceFreshness, TrustInput
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


def assertion_signal(trust_input: TrustInput) -> SignalVerdict:
    """Assertion coverage/validation (SPEC-E14 §3 table row "Assertion").

    The E16 accuracy harness runs assertions and reports pass/fail, but its results are
    not yet persisted per binding, so this signal has nothing to join against at serve
    time — an authored assertion is necessary but not yet sufficient for ``trusted``.
    Every metric caps at ``provisional`` here until harness results are persisted and
    wired in (SPEC-E14 §5, "+ E16 Phase 2").
    """
    if not trust_input.has_assertion:
        return SignalVerdict(
            cap=TrustTier.PROVISIONAL, reason=f"{trust_input.metric}: untested (no assertion)"
        )
    return SignalVerdict(
        cap=TrustTier.PROVISIONAL,
        reason=f"{trust_input.metric}: assertion unverified (pass/fail not yet persisted)",
    )


def static_signals_for(trust_inputs: list[TrustInput]) -> list[SignalVerdict]:
    """The compile-time-available signal set for a set of queried metrics."""
    signals: list[SignalVerdict] = []
    for trust_input in trust_inputs:
        signals.append(provenance_signal(trust_input))
        signals.append(assertion_signal(trust_input))
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
    Inactive when the metric has no known ``source.measure`` binding (composite ratio/
    weighted_avg kinds) or no capping history.
    """
    if trust_input.binding is None:
        return SignalVerdict(cap=None)
    if history.is_capped(trust_input.binding, window_days=window_days):
        return SignalVerdict(cap=TrustTier.CAUTION, reason="outcome: confirmed-wrong")
    return SignalVerdict(cap=None)
