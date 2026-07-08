"""Trust tier model — a category with reasons, not a number (SPEC-E14 §3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["SignalVerdict", "TrustScore", "TrustTier", "tier_meets"]


class TrustTier(StrEnum):
    """The three trust tiers, ordered worst to best (SPEC-E14 §3).

    Declaration order is the trust ordering — :func:`tier_meets` and the scorer both
    rank tiers via ``list(TrustTier).index(...)`` rather than a second, separately
    maintained ordering.
    """

    CAUTION = "caution"
    PROVISIONAL = "provisional"
    TRUSTED = "trusted"


def tier_meets(tier: TrustTier, floor: TrustTier) -> bool:
    """True when ``tier`` is at least as strong as ``floor`` (SPEC-E14 §7 ``min_trust``)."""
    order = list(TrustTier)
    return order.index(tier) >= order.index(floor)


@dataclass(frozen=True, slots=True)
class SignalVerdict:
    """One signal's vote on the tier (SPEC-E14 §3-4).

    ``cap=None`` means the signal is inactive — it neither raises nor lowers the tier
    (SPEC-E14 §4). A non-``None`` ``cap`` caps the answer at that tier or below.
    """

    cap: TrustTier | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TrustScore:
    """A per-answer trust tier with the signals that capped it (SPEC-E14 §3)."""

    tier: TrustTier
    reasons: tuple[str, ...] = ()
