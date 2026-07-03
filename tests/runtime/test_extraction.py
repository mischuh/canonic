"""RuntimeExtractionSkill / make_extraction_skill — the LLM-backed default ExtractionSkill.

Mirrors tests/runtime/test_drafter.py's structure: RuntimeLLMDrafter over the fake litellm
seam is the template for RuntimeExtractionSkill (SPEC-E10, E3 §5 fetch/extract-split
amendment, docs/AMENDMENT-generic-evidence-connector.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from canonic.config import RuntimeConfig
from canonic.connectors.base import UsageHint
from canonic.connectors.evidence import NullExtractionSkill, RawDoc
from canonic.runtime.extraction import RuntimeExtractionSkill, make_extraction_skill
from canonic.runtime.generation import GenerationRuntime

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.config import LLMConfig


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANONIC_LLM_KEY", "secret-token")


def _doc(**overrides: Any) -> RawDoc:
    defaults: dict[str, Any] = {
        "source_ref": "confluence:page:1",
        "title": "Refund window policy",
        "body": "Refunds must be processed within 30 days of the order date.",
    }
    return RawDoc(**{**defaults, **overrides})


class TestRuntimeExtractionSkill:
    async def test_classifies_usage_hint_and_topic_refs(
        self, llm_config: LLMConfig, set_fake: Callable[..., None]
    ) -> None:
        set_fake(content='{"usage_hint": "policy", "topic_refs": ["refunds", "orders"]}')
        skill = RuntimeExtractionSkill(GenerationRuntime(llm_config))

        evidence = await skill.extract(_doc(), source="confluence_space")

        assert evidence.usage_hint == UsageHint.POLICY
        assert evidence.topic_refs == ["refunds", "orders"]
        assert evidence.title == "Refund window policy"
        assert evidence.native_ref == "confluence:page:1"
        assert evidence.source == "confluence_space"
        assert evidence.source_fingerprint is not None

    async def test_unrecognized_usage_hint_defaults_to_reference(
        self, llm_config: LLMConfig, set_fake: Callable[..., None], caplog: pytest.LogCaptureFixture
    ) -> None:
        set_fake(content='{"usage_hint": "not_a_real_hint", "topic_refs": []}')
        skill = RuntimeExtractionSkill(GenerationRuntime(llm_config))

        evidence = await skill.extract(_doc(), source="confluence_space")

        assert evidence.usage_hint == UsageHint.REFERENCE
        assert "not_a_real_hint" in caplog.text

    async def test_missing_topic_refs_defaults_to_empty_list(
        self, llm_config: LLMConfig, set_fake: Callable[..., None]
    ) -> None:
        set_fake(content='{"usage_hint": "caveat"}')
        skill = RuntimeExtractionSkill(GenerationRuntime(llm_config))

        evidence = await skill.extract(_doc(), source="confluence_space")

        assert evidence.usage_hint == UsageHint.CAVEAT
        assert evidence.topic_refs == []

    async def test_uses_extract_task_route(
        self, llm_config: LLMConfig, fake_litellm: dict[str, Any]
    ) -> None:
        skill = RuntimeExtractionSkill(GenerationRuntime(llm_config))
        await skill.extract(_doc(), source="confluence_space")

        # llm_config has no "extract" override configured, so it resolves to the default model
        # via the openai-compatible route — same resolution rule Task.DRAFT/RECONCILE follow.
        assert fake_litellm["model"] == "openai/small-local"


class TestMakeExtractionSkill:
    def test_headless_returns_null_skill(self, llm_config: LLMConfig) -> None:
        skill = make_extraction_skill(llm_config, RuntimeConfig(), headless=True)
        assert isinstance(skill, NullExtractionSkill)

    def test_no_llm_configured_returns_null_skill(self) -> None:
        skill = make_extraction_skill(None, RuntimeConfig(), headless=False)
        assert isinstance(skill, NullExtractionSkill)

    def test_interactive_with_llm_returns_runtime_skill(self, llm_config: LLMConfig) -> None:
        skill = make_extraction_skill(llm_config, RuntimeConfig(), headless=False)
        assert isinstance(skill, RuntimeExtractionSkill)
