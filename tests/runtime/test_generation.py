"""Tests for canon/runtime/generation.py (SPEC-E10 §2, GH-61)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import litellm
import pytest
from pydantic import BaseModel

from canon.config import LLMConfig
from canon.exc import (
    ErrorCode,
    GenerationError,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canon.runtime.generation import GenerationRuntime
from canon.runtime.models import Completion

if TYPE_CHECKING:
    from collections.abc import Callable


class _Grain(BaseModel):
    grain: list[str]


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANON_LLM_KEY", "secret-token")


# --- request construction (a) -------------------------------------------------


async def test_openai_compatible_request_construction(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    runtime = GenerationRuntime(llm_config)
    completion = await runtime.generate("hello", task="draft")

    assert isinstance(completion, Completion)
    # The model is provider-routed via the "openai/" prefix — no per-engine branch.
    assert fake_litellm["model"] == "openai/small-local"
    assert fake_litellm["api_base"] == "http://localhost:11434/v1"
    assert fake_litellm["api_key"] == "secret-token"
    assert fake_litellm["messages"] == [{"role": "user", "content": "hello"}]
    assert completion.model == "openai/small-local"


async def test_task_override_resolves_to_configured_model(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    runtime = GenerationRuntime(llm_config)
    await runtime.generate("hi", task="reconcile")
    assert fake_litellm["model"] == "openai/stronger-model"


async def test_unmapped_task_falls_back_to_default_model(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    runtime = GenerationRuntime(llm_config)
    await runtime.generate("hi", task="unknown")
    assert fake_litellm["model"] == "openai/small-local"


async def test_system_message_is_prepended(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    runtime = GenerationRuntime(llm_config)
    await runtime.generate("body", system="be terse")
    assert fake_litellm["messages"][0] == {"role": "system", "content": "be terse"}


async def test_nullable_key_passes_placeholder(fake_litellm: dict[str, Any]) -> None:
    config = LLMConfig(
        provider="openai_compatible", base_url="http://localhost:11434/v1", model="m"
    )
    await GenerationRuntime(config).generate("hi")
    assert fake_litellm["api_key"] == "not-needed"


# --- structured output (b) ----------------------------------------------------


async def test_structured_output_is_parsed(
    llm_config: LLMConfig, fake_litellm: dict[str, Any], set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"grain": ["order_id"]}')
    runtime = GenerationRuntime(llm_config)
    completion = await runtime.generate("draft", response_model=_Grain)

    assert completion.parsed == {"grain": ["order_id"]}
    # The pydantic model itself is handed to litellm as the response_format.
    assert fake_litellm["response_format"] is _Grain


async def test_plain_completion_has_no_parsed_payload(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content="free prose")
    completion = await GenerationRuntime(llm_config).generate("draft")
    assert completion.parsed is None
    assert completion.text == "free prose"


# --- errors (c) ---------------------------------------------------------------


async def test_invalid_structured_output_raises(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(content='{"not_grain": 1}')
    runtime = GenerationRuntime(llm_config)
    with pytest.raises(StructuredOutputError) as err:
        await runtime.generate("draft", response_model=_Grain)
    assert err.value.code is ErrorCode.STRUCTURED_OUTPUT_INVALID
    assert err.value.exit_code == 16


async def test_unsupported_structured_output_raises(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(raises=litellm.UnsupportedParamsError(message="no", model="m", llm_provider="openai"))
    runtime = GenerationRuntime(llm_config)
    with pytest.raises(StructuredOutputUnsupported) as err:
        await runtime.generate("draft", response_model=_Grain)
    assert err.value.code is ErrorCode.STRUCTURED_OUTPUT_UNSUPPORTED
    assert err.value.exit_code == 17


async def test_provider_failure_raises_generation_error(
    llm_config: LLMConfig, set_fake: Callable[..., None]
) -> None:
    set_fake(
        raises=litellm.APIError(status_code=500, message="boom", llm_provider="openai", model="m")
    )
    runtime = GenerationRuntime(llm_config)
    with pytest.raises(GenerationError) as err:
        await runtime.generate("draft")
    assert err.value.exit_code == 15


def test_non_openai_compatible_provider_rejected() -> None:
    config = LLMConfig(provider="anthropic", base_url="http://x/v1", model="m")
    with pytest.raises(GenerationError):
        GenerationRuntime(config)


# --- repoint property: AC2 ----------------------------------------------------


async def test_repoint_changes_only_base_url_and_key(
    fake_litellm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local → hosted differs only in api_base/api_key; same model, same code path (AC2)."""
    monkeypatch.setenv("HOSTED_KEY", "hosted-token")
    local = LLMConfig(
        provider="openai_compatible",
        base_url="http://localhost:11434/v1",
        model="small-local",
        api_key_ref="env:CANON_LLM_KEY",
    )
    hosted = LLMConfig(
        provider="openai_compatible",
        base_url="https://api.vendor.com/v1",
        model="small-local",
        api_key_ref="env:HOSTED_KEY",
    )

    await GenerationRuntime(local).generate("hi", task="draft")
    local_call = {k: v for k, v in fake_litellm.items() if not k.startswith("_")}

    await GenerationRuntime(hosted).generate("hi", task="draft")
    hosted_call = {k: v for k, v in fake_litellm.items() if not k.startswith("_")}

    differing = {k for k in local_call if local_call[k] != hosted_call.get(k)}
    assert differing == {"api_base", "api_key"}
    assert local_call["model"] == hosted_call["model"] == "openai/small-local"
