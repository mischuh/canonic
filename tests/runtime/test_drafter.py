"""AC1 — an E4 draft succeeds over openai_compatible with no engine-specific code path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from canon.connectors.base import AcquisitionTier, ColumnInfo, RelationSchema, compute_fingerprint
from canon.ingestion.builder import (
    LLM_GRAIN_CONFIDENCE,
    LLM_GRAIN_CONFIDENCE_CEILING,
    ContextBuilder,
)
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


async def test_e4_draft_succeeds_over_runtime(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"grain": ["id"]}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    builder = ContextBuilder(llm_drafter=drafter)

    result = await builder.build([_evidence(_schema_without_pk())])

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.drafted_by is DraftedBy.LLM
    assert proposal.content["grain"] == ["id"]
    assert proposal.confidence == LLM_GRAIN_CONFIDENCE


async def test_draft_uses_openai_compatible_path(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    await ContextBuilder(llm_drafter=drafter).build([_evidence(_schema_without_pk())])

    # The draft task resolves to the default model via the openai-compatible route.
    assert fake_litellm["model"] == "openai/small-local"
    assert fake_litellm["api_base"] == "http://localhost:11434/v1"


async def test_draft_grain_reads_new_field_names_and_reasoning(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(
        content=(
            '{"inferred_grain": ["id"], "confidence_score": 0.7, '
            '"reasoning": "id is unique and not null"}'
        )
    )
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    builder = ContextBuilder(llm_drafter=drafter)

    result = await builder.build([_evidence(_schema_without_pk())])

    proposal = result.proposals[0]
    assert proposal.content["grain"] == ["id"]
    assert proposal.confidence == 0.7
    assert proposal.content["meta"]["grain_reasoning"] == "id is unique and not null"


async def test_draft_grain_caps_overconfident_self_report(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"inferred_grain": ["id"], "confidence_score": 0.99}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    builder = ContextBuilder(llm_drafter=drafter)

    result = await builder.build([_evidence(_schema_without_pk())])

    assert result.proposals[0].confidence == LLM_GRAIN_CONFIDENCE_CEILING


async def test_draft_grain_missing_confidence_falls_back_to_default(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"inferred_grain": ["id"]}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    builder = ContextBuilder(llm_drafter=drafter)

    result = await builder.build([_evidence(_schema_without_pk())])

    assert result.proposals[0].confidence == LLM_GRAIN_CONFIDENCE


async def test_draft_grain_empty_grain_yields_zero_confidence(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"inferred_grain": [], "confidence_score": 0.9}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))
    builder = ContextBuilder(llm_drafter=drafter)

    result = await builder.build([_evidence(_schema_without_pk())])

    assert result.proposals[0].content["grain"] == []
    assert result.proposals[0].confidence == 0.0


# ---------------------------------------------------------------------------
# Dimension label/alias drafting (bootstrap task expansion)
# ---------------------------------------------------------------------------


def _status_dimension() -> list[dict[str, Any]]:
    return [{"name": "status", "column": "status"}]


async def test_draft_dimension_labels_parses_response(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(
        content=(
            '{"dimensions": [{"name": "status", "label": "Order Status", '
            '"aliases": ["order_state"], "confidence": 0.9, "reasoning": "categorical field"}]}'
        )
    )
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))

    drafts = await drafter.draft_dimension_labels(_schema_without_pk(), _status_dimension())

    assert len(drafts) == 1
    assert drafts[0].name == "status"
    assert drafts[0].label == "Order Status"
    assert drafts[0].aliases == ["order_state"]
    assert drafts[0].confidence == 0.9


async def test_draft_dimension_labels_defaults_missing_fields(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"dimensions": [{"name": "status"}]}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))

    drafts = await drafter.draft_dimension_labels(_schema_without_pk(), _status_dimension())

    assert drafts[0].label is None
    assert drafts[0].aliases == []
    assert drafts[0].confidence == 0.0


async def test_draft_dimension_labels_empty_dimensions_list(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"dimensions": []}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))

    assert await drafter.draft_dimension_labels(_schema_without_pk(), _status_dimension()) == []


def test_dimension_label_prompt_lists_table_and_dimensions() -> None:
    from canon.runtime.drafter import _dimension_label_prompt

    prompt = _dimension_label_prompt(_schema_without_pk(), _status_dimension())

    assert "analytics.events" in prompt
    assert "status (column: status)" in prompt
    assert '"dimensions"' in prompt


# ---------------------------------------------------------------------------
# FK-less schema-join drafting (star/snowflake bootstrap task expansion)
# ---------------------------------------------------------------------------


def _dim_categories() -> RelationSchema:
    cols = [
        ColumnInfo(name="category_key", type="int", nullable=False, position=1),
        ColumnInfo(name="label", type="string", nullable=True, position=2),
    ]
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.dim_categories",
        kind="table",
        columns=cols,
        primary_key=["category_key"],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, ["category_key"], []),
    )


async def test_draft_schema_joins_parses_response(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(
        content=(
            '{"joins": [{"column": "category_key", "to": "dim_categories", '
            '"to_column": "category_key", "confidence": 0.8, "reasoning": "name match"}]}'
        )
    )
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))

    drafts = await drafter.draft_schema_joins(
        _schema_without_pk(), ["category_key"], {"dim_categories": _dim_categories()}
    )

    assert len(drafts) == 1
    assert drafts[0].column == "category_key"
    assert drafts[0].to == "dim_categories"
    assert drafts[0].to_column == "category_key"
    assert drafts[0].confidence == 0.8


async def test_draft_schema_joins_empty_joins_list(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"joins": []}')
    drafter = RuntimeLLMDrafter(GenerationRuntime(llm_config))

    drafts = await drafter.draft_schema_joins(
        _schema_without_pk(), ["category_key"], {"dim_categories": _dim_categories()}
    )

    assert drafts == []


def test_schema_join_prompt_lists_candidates_and_other_tables() -> None:
    from canon.runtime.drafter import _schema_join_prompt

    prompt = _schema_join_prompt(
        _schema_without_pk(), ["category_key"], {"dim_categories": _dim_categories()}
    )

    assert "analytics.events" in prompt
    assert "category_key" in prompt
    assert "dim_categories: category_key, label" in prompt
    assert '"joins"' in prompt
