"""LLM provider registry — single source of truth shared by config validation
(``canonic/config.py``) and the litellm-backed runtime (``canonic/runtime/generation.py``).

Dependency-light (stdlib only), mirroring ``canonic/airgap.py``: ``canonic.config`` can
enforce provider rules at ``canonic.yaml`` load time without importing litellm.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["CredentialMode", "ProviderSpec", "PROVIDERS"]


class CredentialMode(StrEnum):
    """Whether a provider needs, forbids, or optionally accepts an ``api_key_ref``."""

    #: A key may or may not be needed (local/self-hosted servers).
    OPTIONAL = "optional"
    #: A bearer-token API key is always required.
    REQUIRED = "required"
    #: Authentication is handled entirely outside ``api_key_ref`` (e.g. a device-code
    #: flow the provider's client manages on its own); configuring one is rejected so
    #: it never looks like it does something it doesn't.
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Everything Canonic needs to route one ``llm.provider`` value through litellm."""

    #: litellm routes purely on this model-string prefix (SPEC-E10 §2) — e.g.
    #: ``f"{litellm_prefix}/{model}"``.
    litellm_prefix: str
    #: Whether ``llm.base_url`` must be set (local/self-hosted runtimes have no
    #: fixed default endpoint for litellm to fall back on).
    requires_base_url: bool
    credential_mode: CredentialMode
    #: Host checked for air-gapped egress when no explicit ``base_url`` is configured.
    #: Hosted providers always call a fixed public endpoint, so there is still
    #: something to allowlist-check even without an override. ``None`` only for
    #: providers where ``requires_base_url`` is True (there is always a base_url then).
    default_host: str | None = None


#: Known ``llm.provider`` values. ``openai_compatible`` is the original local/self-hosted
#: path (Ollama, vLLM, LM Studio, llama.cpp, TGI, or any hosted OpenAI-compatible
#: endpoint reached via an explicit ``base_url``); the others are native hosted APIs
#: reached via litellm's own default endpoint unless ``base_url`` overrides it.
PROVIDERS: dict[str, ProviderSpec] = {
    "openai_compatible": ProviderSpec(
        litellm_prefix="openai",
        requires_base_url=True,
        credential_mode=CredentialMode.OPTIONAL,
    ),
    "openai": ProviderSpec(
        litellm_prefix="openai",
        requires_base_url=False,
        credential_mode=CredentialMode.REQUIRED,
        default_host="api.openai.com",
    ),
    "anthropic": ProviderSpec(
        litellm_prefix="anthropic",
        requires_base_url=False,
        credential_mode=CredentialMode.REQUIRED,
        default_host="api.anthropic.com",
    ),
    "github_copilot": ProviderSpec(
        litellm_prefix="github_copilot",
        requires_base_url=False,
        credential_mode=CredentialMode.FORBIDDEN,
        default_host="api.githubcopilot.com",
    ),
}
