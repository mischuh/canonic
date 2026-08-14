"""Diagnostic bundle export — one shareable file for bug reports (SPEC-onboarding §8-9).

Assembles version info, the onboarding funnel state, and an event-log summary into a
single JSON payload a blocked user can attach to a GitHub issue. No query results and
no credentials leave the machine: config ``credentials_ref`` values are always
references, never literal secrets (enforced by ``Connection._reject_literal_secret``),
and served-answer events already carry only hashes and metadata (SPEC-E16 §2). The one
place a literal secret could still slip in is a free-form connector ``params`` entry a
user typed by hand, so ``params`` is redacted defensively.
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any

from canonic.contract import CONTRACT_SCHEMA
from canonic.instrumentation.report import build_funnel, build_report, read_events

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.config import CanonicConfig

__all__ = ["build_diagnostic_bundle"]

_SENSITIVE_KEY_MARKERS = ("password", "secret", "token", "key", "credential", "dsn", "auth")

#: Fields whose value is structurally a *reference* (``env:``/``keyring:``/``file:``),
#: never a literal secret — enforced by ``Connection._reject_literal_secret`` — so they
#: are exempt from the substring match even though their name contains "credential".
_SAFE_REFERENCE_KEYS = frozenset({"credentials_ref", "api_key_ref", "token_ref"})


def _redact(value: Any) -> Any:
    """Recursively mask dict values whose key looks like it could hold a secret."""
    if isinstance(value, dict):
        return {
            k: (
                _redact(v)
                if k in _SAFE_REFERENCE_KEYS
                else (
                    "***REDACTED***"
                    if any(marker in k.lower() for marker in _SENSITIVE_KEY_MARKERS)
                    else _redact(v)
                )
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _canonic_version() -> str:
    try:
        return _pkg_version("canonic")
    except PackageNotFoundError:
        return "unknown"


def build_diagnostic_bundle(
    root: Path, config: CanonicConfig | None, config_error: str | None
) -> dict[str, Any]:
    """Assemble the diagnostic bundle payload for ``canonic audit --bundle``.

    ``config`` is None when ``canonic.yaml`` failed to load; ``config_error`` then
    carries the reason so the bundle still explains why no config section is present.
    """
    events = read_events(root, kind="served_answer")
    funnel_events = read_events(root, kind="funnel_milestone")
    report = build_report(events, recent=20)
    funnel = build_funnel(funnel_events)

    config_payload = (
        _redact(config.model_dump(mode="json", exclude_none=True)) if config is not None else None
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "canonic_version": _canonic_version(),
        "contract_schema": CONTRACT_SCHEMA,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "config": config_payload,
        "config_error": config_error,
        "funnel": funnel.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
    }
