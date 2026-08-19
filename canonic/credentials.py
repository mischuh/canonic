"""Resolution of ``credentials_ref`` values into concrete secrets.

The config layer (``canonic/config.py``) validates that every secret is expressed
as a *reference* — ``env:``, ``keyring:``, ``file:`` or ``provider:`` — never a
literal. This module turns such a reference into the actual secret at connection
time.

Two shapes of credential live here:

``env:`` / ``keyring:`` / ``file:``
    Static. Resolved once to a fixed string that stays valid until an operator
    rotates it by hand. ``keyring:``/``file:`` are not implemented yet and raise a
    clear :class:`CredentialError`.

``provider:<name>``
    Dynamic. ``<name>`` selects a registered :class:`CredentialProvider` — the same
    dispatch shape ``ConnectorFactory`` uses for connector types — which fetches a
    short-lived credential (Redshift IAM, Snowflake OAuth, GCP ADC) that carries its
    own expiry. Caching and refresh live once, in :class:`CachingCredentialProvider`;
    no provider re-implements expiry math.

Connectors consume a :class:`CredentialSource` rather than a bare string, because a
provider-backed credential must be resolved on *every* connect: a DSN built at
startup goes stale mid-session for a 15-minute IAM token.

Nothing here is ever written to disk. A fetched credential lives in memory for the
lifetime of the process that fetched it.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from canonic.exc import CredentialError, UnknownCredentialProvider

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "CachingCredentialProvider",
    "CredentialProvider",
    "CredentialProviderRegistry",
    "CredentialSource",
    "ResolvedCredential",
    "StaticCredential",
    "credential_source",
    "default_provider_registry",
    "resolve_credential",
]

#: How long before ``expires_at`` a cached credential is treated as stale. A fetch
#: plus a connect handshake has to fit inside this window, so a credential is never
#: handed to the driver with only milliseconds of life left.
REFRESH_SKEW = timedelta(seconds=60)

_STATIC_SCHEMES = ("env", "keyring", "file")


@dataclass(frozen=True)
class ResolvedCredential:
    """A credential value together with the point at which it stops being valid.

    ``expires_at`` of ``None`` means "effectively static, no refresh needed" — what a
    static ``env:``/``file:``/``keyring:`` ref produces.

    ``username`` lets a provider override the connection's configured user. Redshift
    IAM needs it: ``GetClusterCredentials`` returns a temporary ``DbUser`` (``IAM:…``)
    alongside the password, and that user, not ``params["db_user"]``, is what the
    warehouse authenticates.
    """

    value: str
    expires_at: datetime | None = None
    username: str | None = None

    def is_fresh(self, *, now: datetime, skew: timedelta = REFRESH_SKEW) -> bool:
        """Whether this credential can still be handed out at ``now``."""
        if self.expires_at is None:
            return True
        return now + skew < self.expires_at


class CredentialProvider(Protocol):
    """Fetches a fresh short-lived credential from an external issuer.

    Implementations do the vendor call and nothing else: no caching, no expiry
    arithmetic, no retry policy. :class:`CachingCredentialProvider` wraps every
    provider and owns all of that centrally.
    """

    def get(self) -> ResolvedCredential:
        """Fetch a fresh credential. Called by the caching layer, not by connectors."""
        ...


class CredentialSource(Protocol):
    """What a connector holds instead of a resolved secret string.

    Static and provider-backed credentials share this surface so a connector has one
    code path. :meth:`arefresh` is where any network fetch happens; :meth:`cached_value`
    is non-blocking and safe to call from a driver hook on the event loop.
    """

    @property
    def is_dynamic(self) -> bool:
        """Whether the value can change between connects."""
        ...

    def cached_value(self) -> ResolvedCredential:
        """Return the currently held credential without performing any I/O."""
        ...

    async def arefresh(self) -> None:
        """Fetch a new credential if the held one is stale; a no-op when it is fresh."""
        ...


class StaticCredential:
    """A ``env:``/``keyring:``/``file:`` credential, resolved once at construction."""

    def __init__(self, value: str) -> None:
        self._credential = ResolvedCredential(value=value)

    @property
    def is_dynamic(self) -> bool:
        return False

    def cached_value(self) -> ResolvedCredential:
        return self._credential

    async def arefresh(self) -> None:
        """No-op: a static credential has nothing to refresh."""


class CachingCredentialProvider:
    """Holds the last :class:`ResolvedCredential` and refreshes it shortly before expiry.

    This is the single place caching and expiry live. Every provider (AWS, Snowflake,
    GCP, …) plugs into this same wrapper.

    A failed refresh propagates and clears nothing: the caller sees the provider's
    error rather than silently continuing on a credential that is about to expire
    (SPEC amendment S4). Retry policy, if a provider wants one, belongs in that
    provider's ``get()``.
    """

    def __init__(
        self,
        provider: CredentialProvider,
        *,
        skew: timedelta = REFRESH_SKEW,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._skew = skew
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cached: ResolvedCredential | None = None
        # Guards the fetch so concurrent connects on one engine issue a single call.
        self._lock = threading.Lock()

    @property
    def is_dynamic(self) -> bool:
        return True

    def cached_value(self) -> ResolvedCredential:
        """Return the held credential.

        Raises :class:`CredentialError` if nothing has been fetched yet — callers go
        through :meth:`arefresh` (or :meth:`fetch_if_stale`) before every connect, so
        an empty cache here means a connector skipped that step.
        """
        cached = self._cached
        if cached is None:
            raise CredentialError(
                "no credential has been fetched yet; refresh before reading the cached value"
            )
        return cached

    def fetch_if_stale(self) -> ResolvedCredential:
        """Fetch a fresh credential unless the held one is still good. Blocking."""
        with self._lock:
            cached = self._cached
            if cached is not None and cached.is_fresh(now=self._clock(), skew=self._skew):
                return cached
            fetched = self._provider.get()
            if not fetched.value.strip():
                raise CredentialError(
                    f"credential provider {type(self._provider).__name__} returned an empty value"
                )
            self._cached = fetched
            return fetched

    async def arefresh(self) -> None:
        """Refresh off the event loop; provider calls are blocking vendor SDK I/O."""
        await asyncio.to_thread(self.fetch_if_stale)


class CredentialProviderRegistry:
    """Name → :class:`CredentialProvider` dispatch (parallel to ``ConnectorFactory``).

    The registry maps a ``provider:<name>`` ref onto a factory that builds the provider
    from the connection's non-secret ``params``. An unregistered name raises
    :class:`~canonic.exc.UnknownCredentialProvider` listing what is registered — no
    silent fallback, mirroring ``UnknownConnectorType``.
    """

    def __init__(self) -> None:
        self._registry: dict[str, Callable[[Mapping[str, Any]], CredentialProvider]] = {}
        self._builtins_loaded = False

    def register(
        self, name: str, factory: Callable[[Mapping[str, Any]], CredentialProvider]
    ) -> None:
        """Register ``factory`` under ``name``, replacing any prior registration."""
        self._registry[name] = factory

    def create(self, name: str, params: Mapping[str, Any]) -> CredentialProvider:
        """Build the provider registered as ``name`` from a connection's ``params``."""
        self._load_builtins()
        factory = self._registry.get(name)
        if factory is None:
            raise UnknownCredentialProvider(name, known=sorted(self._registry))
        return factory(params)

    def registered_names(self) -> list[str]:
        """Return the sorted list of registered provider names."""
        self._load_builtins()
        return sorted(self._registry)

    def _load_builtins(self) -> None:
        """Import the builtin providers on first use.

        Deferred because the providers live under ``canonic.connectors``, which already
        imports this module — resolving it at import time would be a cycle. Registering
        here rather than at connector-factory construction keeps a bare
        ``credential_source()`` call working without importing the connector layer.
        """
        if self._builtins_loaded:
            return
        # Set before importing: the import registers into this same registry, and a
        # provider module that itself touched the registry would otherwise recurse.
        self._builtins_loaded = True
        from canonic.connectors.credential_providers import register_builtins

        register_builtins(self)


#: Builtin provider registry. Downstream code adds providers with one register() call.
default_provider_registry = CredentialProviderRegistry()


def _split_ref(ref: str | None) -> tuple[str, str]:
    """Split a ``credentials_ref`` into ``(scheme, target)``, validating its shape."""
    if ref is None:
        raise CredentialError("credentials_ref is required for this connection but was not set")
    scheme, sep, target = ref.partition(":")
    if not sep:
        raise CredentialError(
            f"malformed credentials_ref {ref!r}: "
            "expected 'env:…', 'file:…', 'keyring:…' or 'provider:…'"
        )
    return scheme, target


def resolve_credential(ref: str | None) -> str:
    """Resolve a *static* ``credentials_ref`` into its secret value.

    Args:
        ref: A reference of the form ``env:VAR``, ``keyring:…`` or ``file:…``.
            ``None`` is rejected: ``credentials_ref`` is optional in config (file-based
            connectors like dbt need no secret), but a connector that calls this requires
            one, so a missing ref is a clear configuration error rather than a crash.

    Returns:
        The resolved secret.

    Raises:
        CredentialError: If the reference is missing, its scheme is unsupported or
            malformed, the referenced secret cannot be found, or the ref is a
            ``provider:`` ref. Provider refs resolve to a credential that expires, so a
            caller that freezes one string for the life of the process would silently
            keep using a dead credential; those callers must go through
            :func:`credential_source` instead.
    """
    scheme, target = _split_ref(ref)

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

    if scheme == "provider":
        raise CredentialError(
            f"credentials_ref {ref!r} names a dynamic credential provider, which this caller "
            "cannot use: a provider credential expires and must be re-resolved on every "
            "connect. Use a static env:/keyring:/file: reference here."
        )

    raise CredentialError(f"unknown credentials_ref scheme {scheme!r} in {ref!r}")


def credential_source(
    ref: str | None,
    *,
    params: Mapping[str, Any] | None = None,
    registry: CredentialProviderRegistry | None = None,
) -> CredentialSource:
    """Build the :class:`CredentialSource` for ``ref``.

    Static refs resolve immediately, so a bad ``env:`` ref still fails at construction
    exactly as it did before. A ``provider:`` ref performs no I/O here — the first fetch
    happens on the first connect, which is what keeps the credential fresh.

    Args:
        ref: The connection's ``credentials_ref``.
        params: The connection's non-secret ``params``, passed to the provider so it
            knows *how* to fetch (region, cluster id, db user). Ignored for static refs.
        registry: Provider registry to resolve ``provider:`` names against; defaults to
            :data:`default_provider_registry`.

    Raises:
        CredentialError: On a missing, malformed, or unsupported reference.
        UnknownCredentialProvider: When a ``provider:`` name is not registered.
    """
    scheme, target = _split_ref(ref)
    if scheme in _STATIC_SCHEMES:
        return StaticCredential(resolve_credential(ref))
    if scheme == "provider":
        if not target:
            raise CredentialError("provider: credentials_ref is missing a provider name")
        active = registry if registry is not None else default_provider_registry
        return CachingCredentialProvider(active.create(target, params or {}))
    raise CredentialError(f"unknown credentials_ref scheme {scheme!r} in {ref!r}")
