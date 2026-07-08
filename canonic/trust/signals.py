"""Pluggable trust signals — each a pure function from raw inputs to a SignalVerdict.

Static signals (provenance, assertion coverage) are available from E14 v1 onward. Signals
not yet backed by real data are deliberately omitted here rather than faked, per SPEC-E14
§4 ("when a source isn't online yet ... that signal is simply inactive"):

- Assertion *pass/fail* and outcome history need the E16 accuracy harness and E11 outcome
  aggregation, neither of which exist yet.
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

__all__ = [
    "assertion_signal",
    "finality_signal",
    "freshness_signal",
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

    v1 has no execution harness (E16), so a "passing" verdict can never be confirmed —
    an authored assertion is necessary but not yet sufficient for ``trusted``. Every
    metric caps at ``provisional`` here until E16 supplies a real pass/fail signal
    (SPEC-E14 §5, "+ E16 Phase 2").
    """
    if not trust_input.has_assertion:
        return SignalVerdict(
            cap=TrustTier.PROVISIONAL, reason=f"{trust_input.metric}: untested (no assertion)"
        )
    return SignalVerdict(
        cap=TrustTier.PROVISIONAL,
        reason=f"{trust_input.metric}: assertion unverified (pass/fail pending E16)",
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
