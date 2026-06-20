"""Resolution of ``credentials_ref`` values into concrete secrets.

The config layer (``canon/config.py``) validates that every secret is expressed
as a *reference* — ``env:``, ``keyring:`` or ``file:`` — never a literal. This
module turns such a reference into the actual secret at connection time.

GH-4 scope: only ``env:`` references are resolved; ``file:`` and ``keyring:``
raise a clear :class:`CredentialError` until later phases implement them.
"""

from __future__ import annotations

import os

from canon.exc import CredentialError

__all__ = ["resolve_credential"]


def resolve_credential(ref: str | None) -> str:
    """Resolve a ``credentials_ref`` into its secret value.

    Args:
        ref: A reference of the form ``env:VAR``, ``keyring:…`` or ``file:…``.
            ``None`` is rejected: ``credentials_ref`` is optional in config (file-based
            connectors like dbt need no secret), but a connector that calls this requires
            one, so a missing ref is a clear configuration error rather than a crash.

    Returns:
        The resolved secret.

    Raises:
        CredentialError: If the reference is missing, its scheme is unsupported or
            malformed, or the referenced secret cannot be found.
    """
    if ref is None:
        raise CredentialError("credentials_ref is required for this connection but was not set")
    scheme, sep, target = ref.partition(":")
    if not sep:
        raise CredentialError(
            f"malformed credentials_ref {ref!r}: expected 'env:…', 'file:…' or 'keyring:…'"
        )

    if scheme == "env":
        if not target:
            raise CredentialError("env: credentials_ref is missing a variable name")
        try:
            value = os.environ[target]
        except KeyError as exc:
            raise CredentialError(f"environment variable {target!r} is not set") from exc
        if not value.strip():
            raise CredentialError(f"environment variable {target!r} is set but empty")
        return value

    if scheme in ("file", "keyring"):
        raise CredentialError(
            f"{scheme}: credentials_ref is not yet supported (GH-4 scope: env: only)"
        )

    raise CredentialError(f"unknown credentials_ref scheme {scheme!r} in {ref!r}")
