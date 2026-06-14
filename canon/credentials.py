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


def resolve_credential(ref: str) -> str:
    """Resolve a ``credentials_ref`` into its secret value.

    Args:
        ref: A reference of the form ``env:VAR``, ``keyring:…`` or ``file:…``.

    Returns:
        The resolved secret.

    Raises:
        CredentialError: If the reference scheme is unsupported, malformed, or
            the referenced secret cannot be found.
    """
    scheme, sep, target = ref.partition(":")
    if not sep:
        raise CredentialError(
            f"malformed credentials_ref {ref!r}: expected 'env:…', 'file:…' or 'keyring:…'"
        )

    if scheme == "env":
        if not target:
            raise CredentialError("env: credentials_ref is missing a variable name")
        try:
            return os.environ[target]
        except KeyError as exc:
            raise CredentialError(f"environment variable {target!r} is not set") from exc

    if scheme in ("file", "keyring"):
        raise CredentialError(
            f"{scheme}: credentials_ref is not yet supported (GH-4 scope: env: only)"
        )

    raise CredentialError(f"unknown credentials_ref scheme {scheme!r} in {ref!r}")
