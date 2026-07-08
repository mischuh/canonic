"""Answer trust scoring (SPEC-E14) — a per-answer confidence tier with reasons.

The tier reports the quality of the *context* behind an answer (binding provenance,
finality, freshness, validation) — never a claim that the answer is factually true.
"""

from canonic.trust.models import SignalVerdict, TrustScore, TrustTier
from canonic.trust.scorer import TrustScorer

__all__ = ["SignalVerdict", "TrustScore", "TrustScorer", "TrustTier"]
