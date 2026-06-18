"""Tests for canon/runtime/generation.py (SPEC-E10 §2, GH-61)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import litellm
import pytest
from pydantic import BaseModel

from canon.airgap import EgressPolicy
from canon.config import LLMConfig
from canon.exc import (
    AirGappedViolation,
    CredentialError,
    ErrorCode,
    GenerationError,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canon.runtime.generation import GenerationRuntime
from canon.runtime.models import Completion
from canon.runtime.resolver import Task

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
    completion = await runtime.generate("hello", task=Task.DRAFT)

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
    await runtime.generate("hi", task=Task.RECONCILE)
    assert fake_litellm["model"] == "openai/stronger-model"


async def test_task_without_override_resolves_to_default_model(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    # The fixture only overrides `reconcile`; `draft` has no entry, so it uses the default —
    # the documented §3 contract, not a silent swap.
    runtime = GenerationRuntime(llm_config)
    await runtime.generate("hi", task=Task.DRAFT)
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


# --- call-time key resolution (#65, SPEC-E10 §6) ------------------------------


async def test_key_resolved_at_call_time_not_construction(
    llm_config: LLMConfig, fake_litellm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the ref's env var unset, construction still succeeds — the key is only needed
    # at the point of egress, so a missing key fails the call, not the wiring.
    monkeypatch.delenv("CANON_LLM_KEY", raising=False)
    runtime = GenerationRuntime(llm_config)
    with pytest.raises(CredentialError):
        await runtime.generate("hi", task=Task.DRAFT)
    assert len(fake_litellm["_calls"]) == 0


def test_key_never_stored_on_instance(llm_config: LLMConfig) -> None:
    # The secret must not live as instance state where it could leak into a repr/dump.
    runtime = GenerationRuntime(llm_config)
    assert not hasattr(runtime, "_api_key")
    assert "secret-token" not in repr(runtime)
    assert "secret-token" not in str(vars(runtime))


async def test_key_resolved_fresh_each_call(
    llm_config: LLMConfig, fake_litellm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No caching: a changed env var is reflected on the next call.
    runtime = GenerationRuntime(llm_config)
    await runtime.generate("hi", task=Task.DRAFT)
    assert fake_litellm["api_key"] == "secret-token"

    monkeypatch.setenv("CANON_LLM_KEY", "rotated-token")
    await runtime.generate("hi", task=Task.DRAFT)
    assert fake_litellm["api_key"] == "rotated-token"


async def test_required_ref_resolving_to_nothing_fails_at_call(
    llm_config: LLMConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Missing and empty both count as "resolves to nothing"; the message names the
    # variable, never the value.
    runtime = GenerationRuntime(llm_config)

    monkeypatch.delenv("CANON_LLM_KEY", raising=False)
    with pytest.raises(CredentialError, match="CANON_LLM_KEY"):
        await runtime.generate("hi", task=Task.DRAFT)

    monkeypatch.setenv("CANON_LLM_KEY", "   ")
    with pytest.raises(CredentialError, match="CANON_LLM_KEY"):
        await runtime.generate("hi", task=Task.DRAFT)


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


def _api_error() -> litellm.APIError:
    return litellm.APIError(status_code=500, message="boom", llm_provider="openai", model="m")


async def test_provider_failure_raises_generation_error_after_bounded_retries(
    llm_config: LLMConfig, fake_litellm: dict[str, Any], set_fake: Callable[..., None]
) -> None:
    set_fake(raises=_api_error())  # raises on every call
    runtime = GenerationRuntime(llm_config, max_retries=1)
    with pytest.raises(GenerationError) as err:
        await runtime.generate("draft", task=Task.RECONCILE)

    assert err.value.exit_code == 15
    # Bounded: exactly max_retries + 1 attempts, all on the same resolved model (no swap).
    assert len(fake_litellm["_calls"]) == 2
    assert {c["model"] for c in fake_litellm["_calls"]} == {"openai/stronger-model"}


async def test_transient_failure_retried_then_succeeds(
    llm_config: LLMConfig, fake_litellm: dict[str, Any], set_fake: Callable[..., None]
) -> None:
    set_fake(raises=_api_error(), raises_times=2)  # two transient failures, then success
    runtime = GenerationRuntime(llm_config, max_retries=2)
    completion = await runtime.generate("draft", task=Task.RECONCILE)

    assert completion.model == "openai/stronger-model"
    assert len(fake_litellm["_calls"]) == 3
    assert {c["model"] for c in fake_litellm["_calls"]} == {"openai/stronger-model"}


async def test_bad_request_is_not_retried(
    llm_config: LLMConfig, fake_litellm: dict[str, Any], set_fake: Callable[..., None]
) -> None:
    set_fake(raises=litellm.BadRequestError(message="bad", model="m", llm_provider="openai"))
    runtime = GenerationRuntime(llm_config, max_retries=3)
    with pytest.raises(GenerationError):
        await runtime.generate("draft")
    # Deterministic rejection — surfaced on the first attempt, never retried.
    assert len(fake_litellm["_calls"]) == 1


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

    await GenerationRuntime(local).generate("hi", task=Task.DRAFT)
    local_call = {k: v for k, v in fake_litellm.items() if not k.startswith("_")}

    await GenerationRuntime(hosted).generate("hi", task=Task.DRAFT)
    hosted_call = {k: v for k, v in fake_litellm.items() if not k.startswith("_")}

    differing = {k for k in local_call if local_call[k] != hosted_call.get(k)}
    assert differing == {"api_base", "api_key"}
    assert local_call["model"] == hosted_call["model"] == "openai/small-local"


# --- air-gapped call-time enforcement: S3/AC2 ---------------------------------


async def test_air_gapped_policy_allows_local_call(
    llm_config: LLMConfig, fake_litellm: dict[str, Any]
) -> None:
    runtime = GenerationRuntime(llm_config, policy=EgressPolicy())
    await runtime.generate("hi", task=Task.DRAFT)
    assert len(fake_litellm["_calls"]) == 1


def test_air_gapped_policy_blocks_public_endpoint_at_construction(
    fake_litellm: dict[str, Any],
) -> None:
    hosted = LLMConfig(
        provider="openai_compatible", base_url="https://api.openai.com/v1", model="m"
    )
    with pytest.raises(AirGappedViolation):
        GenerationRuntime(hosted, policy=EgressPolicy())
    # Blocked before any model call leaves the process.
    assert len(fake_litellm["_calls"]) == 0


async def test_no_policy_leaves_behavior_unchanged(
    fake_litellm: dict[str, Any],
) -> None:
    # Default (policy=None) keeps the existing path: a hosted endpoint is callable.
    hosted = LLMConfig(
        provider="openai_compatible", base_url="https://api.vendor.com/v1", model="m"
    )
    await GenerationRuntime(hosted).generate("hi")
    assert len(fake_litellm["_calls"]) == 1
