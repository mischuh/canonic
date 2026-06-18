"""AC1 — an E4 draft succeeds over openai_compatible with no engine-specific code path.

These are sync tests on purpose: ``RuntimeLLMDrafter`` bridges the sync ``LLMDrafter`` seam
to the async runtime with ``asyncio.run``, so they must not run inside an event loop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from canon.connectors.base import AcquisitionTier, ColumnInfo, RelationSchema, compute_fingerprint
from canon.ingestion.builder import LLM_GRAIN_CONFIDENCE, ContextBuilder
from canon.ingestion.models import DraftedBy, EvidenceItem
from canon.runtime.drafter import RuntimeLLMDrafter
from canon.runtime.generation import GenerationRuntime

if TYPE_CHECKING:
    from collections.abc import Callable

    from canon.config import LLMConfig

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANON_LLM_KEY", "secret-token")


def _schema_without_pk() -> RelationSchema:
    cols = [
        ColumnInfo(name="id", type="int", nullable=False, position=1),
        ColumnInfo(name="amount", type="decimal", nullable=True, position=2),
    ]
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.events",
        kind="table",
        columns=cols,
        primary_key=[],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, [], []),
    )


def _evidence(schema: RelationSchema) -> EvidenceItem:
    return EvidenceItem(
        source=schema.connection,
        kind="relation_schema",
        acquisition_tier=AcquisitionTier.LIVE,
        payload=schema.model_dump(mode="json"),
        source_fingerprint=schema.source_fingerprint or "sha256:none",
        observed_at=_NOW,
    )


def test_e4_draft_succeeds_over_runtime(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"grain": ["id"]}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    builder = ContextBuilder(llm_drafter=drafter)

    result = builder.build([_evidence(_schema_without_pk())])

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.drafted_by is DraftedBy.LLM
    assert proposal.content["grain"] == ["id"]
    assert proposal.confidence == LLM_GRAIN_CONFIDENCE


def test_draft_uses_openai_compatible_path(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    ContextBuilder(llm_drafter=drafter).build([_evidence(_schema_without_pk())])

    # The draft task resolves to the default model via the openai-compatible route.
    assert fake_litellm["model"] == "openai/small-local"
    assert fake_litellm["api_base"] == "http://localhost:11434/v1"
