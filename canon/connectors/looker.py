"""Looker evidence connector — looks/dashboards → normalized UsageEvidence (SPEC-E3 §3.3, §5).

Fetches Looker saved Looks via the Looker REST API 4.0 and emits one :class:`UsageEvidence`
per Look.  The ``role`` is derived deterministically from the Look's visibility and view
count: a non-public or infrequent Look is ``alternative``; a public, frequently-viewed Look
is ``trusted_example``.  Neither path produces ``canonical`` — the :class:`UsageRole` enum
has no such member (PRD FR-13).

Version pinning: the connector validates ``looker_api_version`` at ``test_connection`` time
and rejects anything other than API 4.0 so partial ingest from an incompatible server is
never silently accepted (PRD FR-2).

Authentication uses a Bearer token supplied via ``credentials_ref`` (e.g. ``env:LOOKER_API_TOKEN``).
For production use with client-credential flow, obtain a token externally and supply it as an
environment variable; client-credential OAuth is outside the P1 scope (SPEC-E3 §5).

HTTP fetching uses a dependency-injection seam (:class:`LookerLookSource`) so the connector
can be tested without network access.  The default implementation
(:class:`HttpLookerLookSource`) uses ``httpx`` imported lazily; add ``httpx>=0.27``
to project dependencies to use live API access.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from canon.connectors.base import (
    Capability,
    ConnectorBase,
    DocEvidence,
    Health,
    UsageDefinition,
    UsageEvidence,
    UsageRole,
    _usage_fingerprint,
)
from canon.exc import ConnectionError, UnsupportedSourceVersionError

if TYPE_CHECKING:
    from canon.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["LookerConnector", "LookerLookSource", "HttpLookerLookSource"]

# Only API 4.0 is supported; 3.x is end-of-life (SPEC-E3 §5).
SUPPORTED_API_VERSION = "4.0"

# Number of views required (in addition to trust signal) to promote to trusted_example.
_TRUSTED_EXAMPLE_MIN_VIEWS = 10


def _extract_expr(look: dict[str, Any]) -> str:
    """Extract the metric expression from a Looker Look's query.

    Reconstructs a simplified expression from the query's ``measures`` and ``fields``.
    Unmappable shapes produce ``unknown`` with a WARNING.
    """
    look_id = look.get("id", "?")
    query = look.get("query") or {}

    measures = query.get("measures") or []
    fields = query.get("fields") or []

    # Prefer measures (explicit aggregations); fall back to all fields.
    targets = measures if measures else fields
    if not targets:
        logger.warning("looker look %s has no measures or fields; expr=unknown", look_id)
        return "unknown"

    return ", ".join(str(f) for f in targets)


def _extract_references(look: dict[str, Any]) -> list[str]:
    """Extract model+view references from a Looker Look's query.

    Returns ``["<model>.<view>"]`` style refs, which map to the Looker explore/view
    that the Look's query runs against.
    """
    query = look.get("query") or {}
    model = query.get("model") or ""
    view = query.get("view") or ""
    if model and view:
        return [f"{model}.{view}"]
    if model:
        return [model]
    return []


def _extract_last_seen(look: dict[str, Any]) -> datetime | None:
    """Parse the last-updated timestamp from a Looker Look."""
    raw = look.get("updated_at") or look.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _assign_role(look: dict[str, Any]) -> UsageRole:
    """Determine UsageRole from the Look's visibility and view count.

    A public Look that is viewed frequently is promoted to ``trusted_example``.
    All others are ``alternative`` — the conservative, invariant-safe default.
    """
    is_public = bool(look.get("public", False))
    view_count = int(look.get("view_count", 0))
    if is_public and view_count >= _TRUSTED_EXAMPLE_MIN_VIEWS:
        return UsageRole.TRUSTED_EXAMPLE
    return UsageRole.ALTERNATIVE


@runtime_checkable
class LookerLookSource(Protocol):
    """DI seam for fetching raw Looker Look objects."""

    async def list_looks(self) -> list[dict[str, Any]]: ...

    async def api_version(self) -> str: ...


class HttpLookerLookSource:
    """Default look source that calls the live Looker REST API 4.0 via httpx.

    ``httpx`` is imported lazily so the module can be imported without it;
    add ``httpx>=0.27`` to project dependencies before using this class.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def list_looks(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for live Looker API access; "
                "add httpx>=0.27 to your project dependencies"
            ) from exc

        async with httpx.AsyncClient(headers=self._headers()) as client:
            resp = await client.get(
                f"{self._base_url}/api/4.0/looks",
                params={"fields": "id,title,query,view_count,public,updated_at,created_at"},
            )
            resp.raise_for_status()
            return list(resp.json())

    async def api_version(self) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for live Looker API access; "
                "add httpx>=0.27 to your project dependencies"
            ) from exc

        async with httpx.AsyncClient(headers=self._headers()) as client:
            resp = await client.get(f"{self._base_url}/api/4.0/versions")
            resp.raise_for_status()
            data = resp.json()
            # Returns the highest supported API version string, e.g. "4.0"
            return str(data.get("looker_api_version", ""))


class LookerConnector(ConnectorBase):
    """Evidence connector for Looker Looks → normalized UsageEvidence (SPEC-E3 §3.3, §5).

    Looks are extracted as ``UsageEvidence`` with role ``alternative`` or
    ``trusted_example`` — never ``canonical``.  The role is determined deterministically
    from the Look's public visibility flag and view count (no LLM).

    Args:
        connection: Canon connection config supplying ``params.base_url`` and
            ``credentials_ref`` (the Looker Bearer token).
        look_source: Injectable source for testing.  When ``None`` an
            :class:`HttpLookerLookSource` is built from the connection config.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        look_source: LookerLookSource | None = None,
    ) -> None:
        from canon.credentials import resolve_credential

        self._source = connection.id
        params = connection.params
        self._base_url: str = params.get("base_url", "")

        if look_source is not None:
            self._look_source: LookerLookSource = look_source
        else:
            token = resolve_credential(connection.credentials_ref)
            self._look_source = HttpLookerLookSource(self._base_url, token)

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION, Capability.EXTRACT_EVIDENCE]

    async def _assert_supported_version(self) -> str:
        """Fetch and enforce the pinned API version; raise if out of range.

        Returns the detected version on success. Transport failures surface as
        :exc:`ConnectionError`; a version mismatch as :exc:`UnsupportedSourceVersionError`.
        """
        try:
            version = await self._look_source.api_version()
        except Exception as exc:
            raise ConnectionError(f"Looker API unreachable: {exc}") from exc
        if version != SUPPORTED_API_VERSION:
            raise UnsupportedSourceVersionError(
                "Looker API", detected=version, supported=SUPPORTED_API_VERSION
            )
        return version

    async def test_connection(self) -> Health:
        """Verify API connectivity and validate the API version is 4.0."""
        try:
            version = await self._assert_supported_version()
        except ConnectionError as exc:
            return Health(status="error", message=str(exc))
        return Health(status="ok", message=f"Looker API {version}")

    async def extract_evidence(self) -> list[DocEvidence | UsageEvidence]:
        """Fetch Looker Looks and return one UsageEvidence per Look.

        Enforces the pinned API version first, raising :exc:`UnsupportedSourceVersionError`
        on an out-of-range server so no partial ingest occurs (SPEC-E3 §6, S5).

        Every Look is emitted — none are dropped.  Unmappable expressions are
        recorded as ``unknown`` with a WARNING so the evidence stream is complete
        (SPEC-E3 §4, S2 AC2 pattern).
        """
        await self._assert_supported_version()
        observed_at = datetime.now(UTC)
        looks = await self._look_source.list_looks()
        evidence: list[DocEvidence | UsageEvidence] = []

        for look in looks:
            look_id = look.get("id", "")
            title = look.get("title", "") or ""
            artifact = f"look:{look_id}"
            expr = _extract_expr(look)
            references = _extract_references(look)
            role = _assign_role(look)
            frequency = int(look.get("view_count", 0))
            last_seen = _extract_last_seen(look)
            native_ref = f"looker:look:{look_id}"
            fingerprint = _usage_fingerprint(artifact, title, expr, references, role.value)

            evidence.append(
                UsageEvidence(
                    source=self._source,
                    artifact=artifact,
                    title=title,
                    defines=UsageDefinition(expr=expr, references=references),
                    role=role,
                    frequency=frequency,
                    last_seen=last_seen,
                    native_ref=native_ref,
                    source_fingerprint=fingerprint,
                    observed_at=observed_at,
                )
            )

        return evidence
