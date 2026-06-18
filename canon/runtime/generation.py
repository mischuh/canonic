"""litellm-backed generation runtime (SPEC-E10 §2).

Turns the configured ``llm`` block into actual model calls behind one interface. The
``openai_compatible`` provider is the first-class path: local runtimes (Ollama, vLLM,
LM Studio, llama.cpp, TGI) and hosted OpenAI-compatible endpoints differ only in
``base_url`` and whether a key is needed — there is no per-engine branch in core logic.
litellm routes purely on the model-string prefix, so "docks in without engine-specific
code" (PRD FR-8) holds structurally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import litellm
from litellm.exceptions import APIError, BadRequestError, UnsupportedParamsError
from pydantic import ValidationError

from canon.credentials import resolve_credential
from canon.exc import (
    GenerationError,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canon.runtime.models import Completion
from canon.runtime.resolver import resolve_model

if TYPE_CHECKING:
    from pydantic import BaseModel

    from canon.config import LLMConfig

__all__ = ["GenerationRuntime"]

#: The one provider E10 #61 tests as first-class; the OpenAI-compatible `/v1` surface.
_OPENAI_COMPATIBLE = "openai_compatible"
#: Placeholder passed to litellm when no key is configured — local servers need none.
_NO_KEY_PLACEHOLDER = "not-needed"


class GenerationRuntime:
    """One generation interface over litellm (SPEC-E10 §2, §8).

    Constructed from an :class:`~canon.config.LLMConfig`; ``generate`` resolves a task to a
    model and executes a single litellm call. Schema-constrained structured output is
    supported, with a clear error when an endpoint cannot honor it.
    """

    def __init__(self, config: LLMConfig) -> None:
        if config.provider != _OPENAI_COMPATIBLE:
            # NOTE (#62/#67): other litellm providers are reachable through the same
            # interface; #61 tests only openai_compatible as the first-class path.
            raise GenerationError(
                f"unsupported llm provider {config.provider!r}: "
                f"{_OPENAI_COMPATIBLE!r} is the supported path"
            )
        self._config = config
        # NOTE (#65): resolved eagerly here; #65 moves resolution to call time and
        # guarantees the value is never logged or written to the event log. A nullable
        # ref is valid — local endpoints typically need no key.
        self._api_key: str | None = (
            resolve_credential(config.api_key_ref) if config.api_key_ref else None
        )

    async def generate(
        self,
        prompt: str,
        *,
        task: str | None = None,
        system: str | None = None,
        response_model: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> Completion:
        """Run one generation call, optionally constrained to a JSON schema.

        Args:
            prompt: The user message.
            task: Named task (e.g. ``draft``, ``reconcile``) resolved to a model (§3).
            system: Optional system message.
            response_model: A pydantic model the response must satisfy. When given, the
                call requests schema-constrained output and the result is validated into
                :attr:`Completion.parsed`.
            temperature: Sampling temperature; defaults to 0.0 for reproducible drafting.

        Raises:
            StructuredOutputUnsupported: The endpoint cannot honor schema-constrained output.
            StructuredOutputError: The model returned output that fails schema validation.
            GenerationError: Any other provider/transport failure (no silent fallback).
        """
        model_str = f"openai/{resolve_model(self._config, task)}"
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await litellm.acompletion(
                model=model_str,
                messages=messages,
                api_base=self._config.base_url,
                api_key=self._api_key or _NO_KEY_PLACEHOLDER,
                temperature=temperature,
                response_format=response_model,
            )
        except (UnsupportedParamsError, BadRequestError) as exc:
            if response_model is not None:
                raise StructuredOutputUnsupported(
                    f"endpoint {self._config.base_url!r} (model {model_str!r}) cannot honor "
                    f"JSON-schema-constrained output: {exc}"
                ) from exc
            raise GenerationError(f"generation call to {model_str!r} failed: {exc}") from exc
        except APIError as exc:
            raise GenerationError(f"generation call to {model_str!r} failed: {exc}") from exc

        content = response.choices[0].message.content or ""
        if response_model is None:
            return Completion(text=content, model=model_str)

        try:
            parsed = response_model.model_validate_json(content)
        except ValidationError as exc:
            detail = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            raise StructuredOutputError(
                f"model {model_str!r} returned output that does not satisfy "
                f"{response_model.__name__}: {detail}"
            ) from exc
        return Completion(text=content, parsed=parsed.model_dump(), model=model_str)
