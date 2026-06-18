"""Fixtures for the E10 generation runtime tests.

No mock library: the litellm call is replaced with an ``async`` fake that records its
kwargs and returns a canned response, matching the codebase's Fake-implementation style.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import litellm
import pytest

from canon.config import LLMConfig

if TYPE_CHECKING:
    from collections.abc import Callable


def _response(content: str) -> SimpleNamespace:
    """A minimal stand-in for a litellm ModelResponse exposing the content path used."""
    message = SimpleNamespace(content=content)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


@pytest.fixture
def llm_config() -> LLMConfig:
    """A local openai_compatible config with a keyed ref (env resolved in tests)."""
    return LLMConfig(
        provider="openai_compatible",
        base_url="http://localhost:11434/v1",
        model="small-local",
        api_key_ref="env:CANON_LLM_KEY",
        tasks={"reconcile": "stronger-model"},
    )


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``litellm.acompletion`` with a recording fake.

    Returns the captured-kwargs dict. The canned content defaults to a valid grain payload;
    a test can override behaviour with :func:`set_fake`.
    """
    captured: dict[str, Any] = {}
    state: dict[str, Any] = {"content": '{"grain": ["id"]}', "raises": None, "raises_times": 0}
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        # No clear(): the kwargs keys are stable across calls, so update() overwrites them
        # while preserving the bookkeeping keys (_state, _calls) the fixtures rely on.
        captured.update(kwargs)
        calls.append(dict(kwargs))
        # Raise for the first ``raises_times`` calls (transient), then succeed; an unbounded
        # ``raises`` (raises_times == 0 with a set exception) raises on every call.
        if state["raises"] is not None and (
            state["raises_times"] == 0 or len(calls) <= state["raises_times"]
        ):
            raise state["raises"]
        return _response(state["content"])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    captured["_state"] = state
    captured["_calls"] = calls
    return captured


@pytest.fixture
def set_fake(fake_litellm: dict[str, Any]) -> Callable[..., None]:
    """Helper to set the fake's canned content or a raised exception."""

    def _set(
        *,
        content: str | None = None,
        raises: BaseException | None = None,
        raises_times: int = 0,
    ) -> None:
        if content is not None:
            fake_litellm["_state"]["content"] = content
        fake_litellm["_state"]["raises"] = raises
        # 0 → raise on every call (unbounded); N → raise for the first N calls, then succeed.
        fake_litellm["_state"]["raises_times"] = raises_times

    return _set
