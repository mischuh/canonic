"""Builtin :class:`~canonic.credentials.CredentialProvider` implementations.

A provider fetches a short-lived credential from a cloud issuer. It does the vendor
call and nothing else — caching, expiry arithmetic and refresh live once in
``canonic.credentials.CachingCredentialProvider``.

:func:`register_builtins` is called lazily by
:class:`~canonic.credentials.CredentialProviderRegistry` on first use, which is why
this package may import ``canonic.credentials`` freely: by the time it runs, that
module is fully initialized.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.connectors.credential_providers.aws_iam_redshift import (
    make_aws_iam_redshift_provider,
)

if TYPE_CHECKING:
    from canonic.credentials import CredentialProviderRegistry

__all__ = ["register_builtins"]


def register_builtins(registry: CredentialProviderRegistry) -> None:
    """Register every builtin provider into ``registry``.

    A new provider adds one ``register()`` call here, the same seam
    ``_build_default_factory`` gives connector types.
    """
    registry.register("aws-iam-redshift", make_aws_iam_redshift_provider)
