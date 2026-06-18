"""Fixtures for the E10 generation runtime tests.

``fake_litellm`` and ``set_fake`` live in the root ``tests/conftest.py`` (project-wide).
This module adds the runtime-specific ``llm_config`` fixture.
"""

from __future__ import annotations

import pytest

from canon.config import LLMConfig


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
