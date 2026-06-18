"""Optional live check against a real OpenAI-compatible endpoint (SPEC-E10 S1-AC1).

Skipped unless ``CANON_LLM_BASE_URL`` points at a running server (e.g. a local Ollama,
llama.cpp, or vLLM ``/v1``). This is the literal AC1 manual proof: a real draft over the
``openai_compatible`` path with no engine-specific code. Configure with:

    CANON_LLM_BASE_URL=http://localhost:11434/v1 \
    CANON_LLM_MODEL=llama3.2 \
    pytest -m integration tests/runtime/test_integration.py
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from canon.config import LLMConfig
from canon.runtime.generation import GenerationRuntime

_BASE_URL = os.environ.get("CANON_LLM_BASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _BASE_URL, reason="set CANON_LLM_BASE_URL to run the live check"),
]


class _Grain(BaseModel):
    grain: list[str]


def _config() -> LLMConfig:
    return LLMConfig(
        provider="openai_compatible",
        base_url=_BASE_URL or "",
        model=os.environ.get("CANON_LLM_MODEL", "llama3.2"),
        api_key_ref="env:CANON_LLM_KEY" if os.environ.get("CANON_LLM_KEY") else None,
    )


async def test_live_plain_generation() -> None:
    completion = await GenerationRuntime(_config()).generate(
        "Reply with the single word: ok.", task="draft"
    )
    assert completion.text.strip()


async def test_live_structured_output() -> None:
    runtime = GenerationRuntime(_config())
    completion = await runtime.generate(
        "Columns: order_id (int), line_no (int), sku (string). "
        'Return the grain as {"grain": [<columns>]}.',
        task="draft",
        response_model=_Grain,
    )
    assert completion.parsed is not None
    assert isinstance(completion.parsed["grain"], list)
