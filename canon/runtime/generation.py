"""litellm-backed generation runtime (SPEC-E10 §2).

Turns the configured ``llm`` block into actual model calls behind one interface. The
``openai_compatible`` provider is the first-class path: local runtimes (Ollama, vLLM,
LM Studio, llama.cpp, TGI) and hosted OpenAI-compatible endpoints differ only in
``base_url`` and whether a key is needed — there is no per-engine branch in core logic.
litellm routes purely on the model-string prefix, so "docks in without engine-specific
code" (PRD FR-8) holds structurally.
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Any

import litellm
from litellm.exceptions import APIError, BadRequestError, UnsupportedParamsError
from pydantic import ValidationError

from canon.credentials import resolve_credential
from canon.exc import (
    GenerationError,
    RetriesExhausted,
    StructuredOutputError,
    StructuredOutputUnsupported,
)
from canon.runtime.models import Completion, Usage
from canon.runtime.resolver import Task, resolve_model

if TYPE_CHECKING:
    from pydantic import BaseModel

    from canon.airgap import EgressPolicy
    from canon.config import LLMConfig

__all__ = ["GenerationRuntime"]


def _read_usage(response: Any, *, calls: int, latency_ms: float) -> Usage:
    """Extract token counts from a litellm response, best-effort (SPEC-E10 §8).

    Any missing or malformed field yields ``None`` rather than failing the call — endpoints
    are not required to report usage. ``calls`` and ``latency_ms`` are always present since
    they are measured locally.
    """

    def _int_or_none(val: Any) -> int | None:
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    raw = getattr(response, "usage", None)
    return Usage(
        prompt_tokens=_int_or_none(getattr(raw, "prompt_tokens", None)),
        completion_tokens=_int_or_none(getattr(raw, "completion_tokens", None)),
        total_tokens=_int_or_none(getattr(raw, "total_tokens", None)),
        calls=calls,
        latency_ms=latency_ms,
    )


#: The one provider E10 #61 tests as first-class; the OpenAI-compatible `/v1` surface.
_OPENAI_COMPATIBLE = "openai_compatible"
#: Placeholder passed to litellm when no key is configured — local servers need none.
_NO_KEY_PLACEHOLDER = "not-needed"
#: Default retry budget for transient provider failures (SPEC-E10 §3, S6).
_DEFAULT_MAX_RETRIES = 2


class GenerationRuntime:
    """One generation interface over litellm (SPEC-E10 §2, §8).

    Constructed from an :class:`~canon.config.LLMConfig`; ``generate`` resolves a task to a
    model and executes a single litellm call. Schema-constrained structured output is
    supported, with a clear error when an endpoint cannot honor it.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        policy: EgressPolicy | None = None,
    ) -> None:
        if config.provider != _OPENAI_COMPATIBLE:
            # NOTE (#62/#67): other litellm providers are reachable through the same
            # interface; #61 tests only openai_compatible as the first-class path.
            raise GenerationError(
                f"unsupported llm provider {config.provider!r}: "
                f"{_OPENAI_COMPATIBLE!r} is the supported path"
            )
        # Air-gapped egress guard (SPEC-E10 §4). When set, the same EgressPolicy that
        # validated config at load time blocks any call to a non-allowlisted host before
        # egress. Construction-time check fails fast even before the first generate().
        # `None` (the default for the test/headless paths) means no enforcement; the E4
        # ingest wiring threads a policy when runtime.air_gapped is set.
        self._policy = policy
        if policy is not None:
            policy.check_url(config.base_url, what="llm.base_url")
        self._config = config
        # Bounded retries on transient provider failures; total attempts = max_retries + 1.
        # Injected here (not on LLMConfig, whose shape E1 owns) so it stays testable.
        self._max_retries = max_retries
        # The api_key_ref is resolved at call time, not here (#65): the secret never lives
        # as instance state, so it cannot leak into a repr/dump/log or the event log. A
        # nullable ref is valid — local endpoints typically need no key.

    def _resolve_api_key(self) -> str:
        """Resolve ``api_key_ref`` to a secret at call time (SPEC-E10 §6, #65).

        Returns the resolved secret, or the local-server placeholder when no ref is
        configured. Raises :class:`~canon.exc.CredentialError` when a required ref
        resolves to nothing — a clear, structured failure, never silent. The returned
        value is used immediately and never retained on the instance.
        """
        if self._config.api_key_ref:
            return resolve_credential(self._config.api_key_ref)
        return _NO_KEY_PLACEHOLDER

    async def generate(
        self,
        prompt: str,
        *,
        task: Task | None = None,
        system: str | None = None,
        response_model: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> Completion:
        """Run one generation call, optionally constrained to a JSON schema.

        The model is resolved from ``task`` once and held across all retry attempts: a
        transient provider failure is retried on the **same** model up to the configured
        bound, then surfaced as a structured :class:`GenerationError` — never a quiet switch
        to a different model (§3, S6). The runtime honors the config it was handed;
        per-invocation override / flag-vs-config precedence is E1's (§9).

        Args:
            prompt: The user message.
            task: Named task (``Task.DRAFT``, ``Task.RECONCILE``) resolved to a model (§3).
            system: Optional system message.
            response_model: A pydantic model the response must satisfy. When given, the
                call requests schema-constrained output and the result is validated into
                :attr:`Completion.parsed`.
            temperature: Sampling temperature; defaults to 0.0 for reproducible drafting.

        Raises:
            StructuredOutputUnsupported: The endpoint cannot honor schema-constrained output.
            StructuredOutputError: The model returned output that fails schema validation.
            RetriesExhausted: A transient provider/transport failure persisted past the
                bounded retry budget (timeout-after-retries; distinct from a deterministic
                one-shot rejection).
            GenerationError: A deterministic provider failure or unsupported-provider error
                (not retried).
        """
        model_str = f"openai/{resolve_model(self._config, task)}"
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Block egress to a non-allowlisted host before the request leaves the process
        # (SPEC-E10 §4, S3/AC2). Re-checked here, not only at construction, so a guard is
        # in place at the exact point of egress.
        if self._policy is not None:
            self._policy.check_url(self._config.base_url, what="llm.base_url")

        # Resolve the key here, at the point of egress (#65): a required ref that resolves
        # to nothing fails now with a clear CredentialError. The secret lives only in this
        # local for the duration of the call — never on the instance, never logged.
        api_key = self._resolve_api_key()

        last_exc: APIError | None = None
        calls = 0
        start = perf_counter()
        for _ in range(self._max_retries + 1):
            try:
                calls += 1
                response = await litellm.acompletion(
                    model=model_str,
                    messages=messages,
                    api_base=self._config.base_url,
                    api_key=api_key,
                    temperature=temperature,
                    response_format=response_model,
                )
                break
            except (UnsupportedParamsError, BadRequestError) as exc:
                # Deterministic rejections — retrying the same request cannot help.
                if response_model is not None:
                    raise StructuredOutputUnsupported(
                        f"endpoint {self._config.base_url!r} (model {model_str!r}) cannot honor "
                        f"JSON-schema-constrained output: {exc}"
                    ) from exc
                raise GenerationError(f"generation call to {model_str!r} failed: {exc}") from exc
            except APIError as exc:
                # Transient transport/provider failure — retry on the same model.
                last_exc = exc
        else:
            raise RetriesExhausted(
                f"generation call to {model_str!r} failed after {calls} attempts: {last_exc}"
            ) from last_exc

        latency_ms = (perf_counter() - start) * 1000
        usage = _read_usage(response, calls=calls, latency_ms=latency_ms)

        content = response.choices[0].message.content or ""
        if response_model is None:
            return Completion(text=content, model=model_str, usage=usage)

        try:
            parsed = response_model.model_validate_json(content)
        except ValidationError as exc:
            detail = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            raise StructuredOutputError(
                f"model {model_str!r} returned output that does not satisfy "
                f"{response_model.__name__}: {detail}"
            ) from exc
        return Completion(text=content, parsed=parsed.model_dump(), model=model_str, usage=usage)
