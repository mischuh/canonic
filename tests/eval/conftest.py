"""Fixtures for the E10 baseline-harness tests (SPEC-E10 §7, GH-66).

No network and no litellm: a stub drafter returns a canned grain or raises a chosen E10 error,
and a stub usage reader reports a fixed token count. This mirrors the codebase's
Fake-implementation style (see tests/runtime/conftest.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canon.config import LLMConfig
from canon.eval.candidates import NamedCandidate
from canon.eval.dataset import GrainCase
from canon.ingestion.builder import GrainDraft

if TYPE_CHECKING:
    from canon.connectors.base import RelationSchema


class StubDrafter:
    """A ``GrainDrafter`` that returns a fixed grain or raises a fixed exception."""

    def __init__(self, *, grain: list[str] | None = None, raises: Exception | None = None) -> None:
        self._grain = grain
        self._raises = raises

    async def draft_grain(self, schema: RelationSchema) -> GrainDraft:  # noqa: ARG002
        if self._raises is not None:
            raise self._raises
        return GrainDraft(grain=list(self._grain) if self._grain is not None else [])


class StubUsageReader:
    """A no-op usage reader that reports a fixed token count."""

    def __init__(self, tokens: int | None = None) -> None:
        self._tokens = tokens

    def __enter__(self) -> StubUsageReader:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def reset(self) -> None:
        return None

    @property
    def last_total_tokens(self) -> int | None:
        return self._tokens


def make_candidate(name: str, model: str) -> NamedCandidate:
    """A candidate over a local openai_compatible config (no key — local server)."""
    return NamedCandidate(
        name=name,
        config=LLMConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:11434/v1",
            model=model,
        ),
    )


@pytest.fixture
def grain_cases() -> list[GrainCase]:
    """A small in-memory labeled set: one surrogate key and one composite grain."""
    return [
        GrainCase.model_validate(
            {
                "relation": "app.orders",
                "columns": [
                    {"name": "id", "type": "int", "nullable": False},
                    {"name": "customer_id", "type": "int", "nullable": False},
                ],
                "expected_grain": ["id"],
            }
        ),
        GrainCase.model_validate(
            {
                "relation": "app.order_items",
                "columns": [
                    {"name": "order_id", "type": "int", "nullable": False},
                    {"name": "product_id", "type": "int", "nullable": False},
                ],
                "expected_grain": ["order_id", "product_id"],
            }
        ),
    ]
