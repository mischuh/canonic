"""Compiler stage 5b acceptance tests for restrict_source guardrail (SPEC-E5-E15 §2.4 S2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.exc import GuardrailBlock

if TYPE_CHECKING:
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import SemanticSource


_AS_OF = datetime(2026, 6, 13, 10, 0, 0, tzinfo=UTC)
# watermark resolves to 2026-06-12T23:59:59-04:00 (business_day - 1 day from 2026-06-13)


class TestS2RestrictSourceAC1:
    """AC1: board_reporting context + window past watermark → GUARDRAIL_BLOCK."""

    def test_blocks_when_upper_bound_past_watermark(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock) as exc_info:
            compile(
                SemanticQuery(
                    metrics=["revenue"],
                    dimensions=["order_date"],
                    filters=["order_date <= '2026-06-20'"],
                    context="board_reporting",
                    as_of=_AS_OF,
                ),
                board_resolver,
                sources,
            )
        assert exc_info.value.exit_code == 8
        assert "T-1" in str(exc_info.value)

    def test_blocks_when_lower_bound_after_watermark(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock):
            compile(
                SemanticQuery(
                    metrics=["revenue"],
                    dimensions=["order_date"],
                    filters=["order_date >= '2026-06-15'"],
                    context="board_reporting",
                    as_of=_AS_OF,
                ),
                board_resolver,
                sources,
            )

    def test_rationale_in_exception_message(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock) as exc_info:
            compile(
                SemanticQuery(
                    metrics=["revenue"],
                    dimensions=["order_date"],
                    filters=["order_date <= '2026-06-20'"],
                    context="board_reporting",
                    as_of=_AS_OF,
                ),
                board_resolver,
                sources,
            )
        assert "Board reporting" in str(exc_info.value)


class TestS2RestrictSourceAC2:
    """AC2: board_reporting context + window entirely ≤ watermark → query succeeds."""

    def test_succeeds_when_upper_bound_within_watermark(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(
                metrics=["revenue"],
                dimensions=["order_date"],
                filters=["order_date <= '2026-06-10'"],
                context="board_reporting",
                as_of=_AS_OF,
            ),
            board_resolver,
            sources,
        )
        assert result.sql

    def test_succeeds_when_upper_bound_equals_watermark_day(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(
                metrics=["revenue"],
                dimensions=["order_date"],
                filters=["order_date <= '2026-06-12'"],
                context="board_reporting",
                as_of=_AS_OF,
            ),
            board_resolver,
            sources,
        )
        assert result.sql


class TestS2RestrictSourceNoop:
    """The guardrail is a no-op when context is absent or different, or no time filter."""

    def test_noop_when_context_absent(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(
                metrics=["revenue"],
                dimensions=["order_date"],
                filters=["order_date <= '2026-06-20'"],
                as_of=_AS_OF,
            ),
            board_resolver,
            sources,
        )
        assert result.sql

    def test_noop_when_different_context(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(
                metrics=["revenue"],
                dimensions=["order_date"],
                filters=["order_date <= '2026-06-20'"],
                context="internal_dashboard",
                as_of=_AS_OF,
            ),
            board_resolver,
            sources,
        )
        assert result.sql

    def test_noop_when_no_time_filter(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(
                metrics=["revenue"],
                dimensions=["order_date"],
                context="board_reporting",
                as_of=_AS_OF,
            ),
            board_resolver,
            sources,
        )
        assert result.sql

    def test_noop_when_no_finality_rule(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """Resolver without a finality rule: restrict_source is inert (no watermark)."""
        result = compile(
            SemanticQuery(
                metrics=["revenue"],
                dimensions=["order_date"],
                filters=["order_date <= '2026-06-20'"],
                context="board_reporting",
                as_of=_AS_OF,
            ),
            resolver,
            sources,
        )
        assert result.sql


class TestS2RestrictSourceDeterminism:
    """Two compiles of the same blocked query both raise GuardrailBlock identically."""

    def test_deterministic_block(
        self, board_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        q = SemanticQuery(
            metrics=["revenue"],
            dimensions=["order_date"],
            filters=["order_date <= '2026-06-20'"],
            context="board_reporting",
            as_of=_AS_OF,
        )
        with pytest.raises(GuardrailBlock) as e1:
            compile(q, board_resolver, sources)
        with pytest.raises(GuardrailBlock) as e2:
            compile(q, board_resolver, sources)
        assert str(e1.value) == str(e2.value)
