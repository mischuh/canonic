"""Tests for watermark evaluation and finality rule validation (SPEC-Fuller-E15 §2, §5.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from canonic.contracts.finality import evaluate_watermark, validate_finality_rule, watermark_to_iso
from canonic.contracts.models import FinalityRule, Realization


class TestEvaluateWatermark:
    def test_business_day_minus_one_returns_end_of_previous_bd(self) -> None:
        tz = "America/New_York"
        # 2026-06-13 is a Saturday; previous business day is 2026-06-12 (Friday)
        # With "business_day - 1 day": base is the previous BD (Fri 2026-06-12);
        # then subtract 1 day → Thu 2026-06-11 end-of-day.
        # But as_of is 2026-06-13 (Saturday) → _prev_business_day → 2026-06-12 (Fri)
        # then - 1 day → 2026-06-11 (Thu) → _prev_business_day → 2026-06-11 (already Thu)
        as_of = datetime(2026, 6, 13, 12, 0, 0, tzinfo=ZoneInfo(tz))
        result = evaluate_watermark("business_day - 1 day", tz, as_of)
        assert result.date().isoformat() == "2026-06-11"
        assert result.hour == 23
        assert result.minute == 59
        assert result.second == 59

    def test_business_day_alone(self) -> None:
        tz = "America/New_York"
        # as_of is Monday 2026-06-15; business_day = Mon 2026-06-15
        as_of = datetime(2026, 6, 15, 9, 0, 0, tzinfo=ZoneInfo(tz))
        result = evaluate_watermark("business_day", tz, as_of)
        assert result.date().isoformat() == "2026-06-15"
        assert result.hour == 23

    def test_spec_example_matches(self) -> None:
        """§2.3 example: watermark '2026-06-12T23:59:59-04:00'.

        2026-06-13 is a Saturday; prev_BD = Fri 2026-06-12.
        "business_day - 1 day" from Mon 2026-06-15:
          base_bd = Mon 2026-06-15 → -1 day = Sun 2026-06-14
          → prev_BD(Sun) = Fri 2026-06-12 → watermark = 2026-06-12T23:59:59.
        """
        tz = "America/New_York"
        as_of = datetime(2026, 6, 15, 10, 0, 0, tzinfo=ZoneInfo(tz))  # Monday
        result = evaluate_watermark("business_day - 1 day", tz, as_of)
        assert result.date().isoformat() == "2026-06-12"
        iso = watermark_to_iso(result)
        assert "2026-06-12T23:59:59" in iso

    def test_unknown_tz_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown timezone"):
            evaluate_watermark("business_day", "Not/AReal_Zone", None)

    def test_unsupported_expression_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported watermark"):
            evaluate_watermark("now() - interval '1 day'", "UTC", None)

    def test_as_of_none_uses_current_time(self) -> None:
        result = evaluate_watermark("business_day - 1 day", "UTC", None)
        assert result.hour == 23
        assert result.second == 59

    def test_result_is_tz_aware(self) -> None:
        tz = "America/New_York"
        as_of = datetime(2026, 6, 13, 10, 0, 0, tzinfo=ZoneInfo(tz))
        result = evaluate_watermark("business_day - 1 day", tz, as_of)
        assert result.tzinfo is not None

    def test_deterministic_same_inputs(self) -> None:
        tz = "UTC"
        as_of = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)
        r1 = evaluate_watermark("business_day - 1 day", tz, as_of)
        r2 = evaluate_watermark("business_day - 1 day", tz, as_of)
        assert r1 == r2


class TestValidateFinalityRule:
    def _rule(self, **kw: object) -> FinalityRule:
        defaults = dict(
            metric="revenue",
            realizations=[
                Realization(
                    source="orders",
                    role="final",
                    watermark="business_day - 1 day",
                    tz="America/New_York",
                ),
                Realization(source="orders_rt", role="provisional"),
            ],
        )
        defaults.update(kw)
        return FinalityRule(**defaults)  # type: ignore[arg-type]

    def test_valid_rule_passes(self) -> None:
        validate_finality_rule(self._rule())

    def test_no_final_raises(self) -> None:
        with pytest.raises(ValidationError, match="exactly one 'final'"):
            FinalityRule(
                metric="revenue",
                realizations=[Realization(source="orders_rt", role="provisional")],
            )

    def test_two_final_raises(self) -> None:
        with pytest.raises(ValidationError, match="exactly one 'final'"):
            FinalityRule(
                metric="revenue",
                realizations=[
                    Realization(
                        source="orders",
                        role="final",
                        watermark="business_day - 1 day",
                        tz="UTC",
                    ),
                    Realization(
                        source="orders_b",
                        role="final",
                        watermark="business_day",
                        tz="UTC",
                    ),
                ],
            )

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(ValidationError, match="unknown role"):
            FinalityRule(
                metric="revenue",
                realizations=[
                    Realization(
                        source="orders",
                        role="authoritative",
                        watermark="business_day",
                        tz="UTC",
                    ),
                ],
            )

    def test_missing_watermark_raises(self) -> None:
        with pytest.raises(ValidationError, match="requires a 'watermark'"):
            FinalityRule(
                metric="revenue",
                realizations=[Realization(source="orders", role="final", tz="UTC")],
            )

    def test_missing_tz_raises(self) -> None:
        with pytest.raises(ValidationError, match="requires a 'tz'"):
            FinalityRule(
                metric="revenue",
                realizations=[
                    Realization(source="orders", role="final", watermark="business_day - 1 day")
                ],
            )

    def test_unknown_tz_raises(self) -> None:
        with pytest.raises(ValidationError, match="unknown timezone"):
            FinalityRule(
                metric="revenue",
                realizations=[
                    Realization(
                        source="orders",
                        role="final",
                        watermark="business_day",
                        tz="Not/Real",
                    )
                ],
            )

    def test_bad_watermark_expression_raises(self) -> None:
        with pytest.raises(ValidationError, match="does not match the supported grammar"):
            FinalityRule(
                metric="revenue",
                realizations=[
                    Realization(
                        source="orders",
                        role="final",
                        watermark="yesterday",
                        tz="UTC",
                    )
                ],
            )

    def test_cross_surface_unknown_source_raises(self) -> None:
        rule = FinalityRule(
            metric="revenue",
            realizations=[
                Realization(
                    source="unknown_src",
                    role="final",
                    watermark="business_day",
                    tz="UTC",
                )
            ],
        )
        with pytest.raises(ValueError, match="not declared in semantics"):
            validate_finality_rule(rule, source_names={"orders", "orders_rt"})

    def test_cross_surface_valid_sources_pass(self) -> None:
        validate_finality_rule(self._rule(), source_names={"orders", "orders_rt"})


class TestWatermarkToIso:
    def test_offset_form_no_utc_label(self) -> None:
        dt = datetime(2026, 6, 12, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))
        iso = watermark_to_iso(dt)
        assert "2026-06-12T23:59:59" in iso
        assert "UTC" not in iso

    def test_naive_becomes_utc(self) -> None:
        dt = datetime(2026, 6, 12, 23, 59, 59)
        iso = watermark_to_iso(dt)
        assert "+00:00" in iso
