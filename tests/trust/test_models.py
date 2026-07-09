"""Tests for the TrustTier ordering and tier_meets helper (SPEC-E14 §3, §7)."""

from __future__ import annotations

from canonic.trust.models import TrustTier, tier_meets


class TestTierMeets:
    def test_equal_tier_meets_floor(self) -> None:
        assert tier_meets(TrustTier.PROVISIONAL, TrustTier.PROVISIONAL) is True

    def test_higher_tier_meets_lower_floor(self) -> None:
        assert tier_meets(TrustTier.TRUSTED, TrustTier.PROVISIONAL) is True
        assert tier_meets(TrustTier.TRUSTED, TrustTier.CAUTION) is True
        assert tier_meets(TrustTier.PROVISIONAL, TrustTier.CAUTION) is True

    def test_lower_tier_does_not_meet_higher_floor(self) -> None:
        assert tier_meets(TrustTier.CAUTION, TrustTier.PROVISIONAL) is False
        assert tier_meets(TrustTier.PROVISIONAL, TrustTier.TRUSTED) is False
        assert tier_meets(TrustTier.CAUTION, TrustTier.TRUSTED) is False
