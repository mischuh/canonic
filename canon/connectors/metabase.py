"""Metabase evidence connector — questions/dashboards → normalized UsageEvidence (SPEC-E3 §3.3, §5).

Fetches Metabase saved questions (cards) via the Metabase REST API (0.48+) and emits one
:class:`UsageEvidence` per question.  The ``role`` is derived deterministically from the
question's trust signal (official collection membership) and view frequency: an unofficial or
infrequent question is ``alternative``; an official, frequently-run question is
``trusted_example``.  Neither path produces ``canonical`` — the :class:`UsageRole` enum has no
such member (PRD FR-13).

Version pinning: the connector validates ``version.tag`` at ``test_connection`` time and
rejects anything below ``MIN_API_VERSION`` so partial ingest from an incompatible server is
never silently accepted (PRD FR-2).

HTTP fetching uses a dependency-injection seam (:class:`MetabaseQuestionSource`) so the
connector can be tested without network access.  The default implementation
(:class:`HttpMetabaseQuestionSource`) uses ``httpx`` imported lazily; add ``httpx>=0.27``
to project dependencies to use live API access.
"""

from __future__ import annotations

import logging
import re
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

if TYPE_CHECKING:
    from canon.config import Connection

logger = logging.getLogger(__name__)

__all__ = ["MetabaseConnector", "MetabaseQuestionSource", "HttpMetabaseQuestionSource"]

# Minimum supported Metabase version (REST API 0.48+ added x-api-key auth).
MIN_API_VERSION = (0, 48)

# Number of views required (in addition to trust signal) to promote to trusted_example.
_TRUSTED_EXAMPLE_MIN_VIEWS = 10


def _parse_version(version_tag: str) -> tuple[int, ...] | None:
    """Parse a Metabase version tag like ``v0.48.7`` or ``v1.2.3`` to a comparable tuple."""
    match = re.match(r"v?(\d+)\.(\d+)", version_tag)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _extract_expr(card: dict[str, Any]) -> str:
    """Extract the metric expression from a Metabase card's dataset_query.

    For native SQL queries the raw SQL is used.  For MBQL (structured) queries the
    aggregation clause is reconstructed as a simplified expression.  Unmappable
    shapes produce a sentinel ``unknown`` string with a warning.
    """
    query = card.get("dataset_query", {})
    qtype = query.get("type", "")
    card_id = card.get("id", "?")

    if qtype == "native":
        sql = query.get("native", {}).get("query", "")
        return sql.strip() or "unknown"

    if qtype == "query":
        inner = query.get("query", {})
        aggregations = inner.get("aggregation", [])
        if not aggregations:
            return "unknown"
        # Reconstruct simplified expr from first aggregation, e.g. ["sum", ["field", 1, ...]] → sum(field)
        parts: list[str] = []
        for agg in aggregations:
            if not isinstance(agg, list) or not agg:
                continue
            func = agg[0] if isinstance(agg[0], str) else "unknown"
            if len(agg) > 1 and isinstance(agg[1], list) and agg[1]:
                field_arg = agg[1]
                if field_arg[0] == "field" and len(field_arg) >= 2:
                    parts.append(f"{func}(field:{field_arg[1]})")
                    continue
            parts.append(func)
        return ", ".join(parts) if parts else "unknown"

    logger.warning(
        "metabase card %s has unrecognized dataset_query type %r; expr=unknown", card_id, qtype
    )
    return "unknown"


def _extract_references(card: dict[str, Any]) -> list[str]:
    """Extract source table references from a Metabase card.

    For MBQL queries reads ``source-table`` id (recorded as ``metabase:table:<id>``).
    For native SQL queries there is no reliable table extraction without a full SQL parser;
    an empty list is returned rather than silently guessing.
    """
    query = card.get("dataset_query", {})
    qtype = query.get("type", "")
    if qtype == "query":
        inner = query.get("query", {})
        table_id = inner.get("source-table")
        if table_id is not None:
            return [f"metabase:table:{table_id}"]
    return []


def _extract_last_seen(card: dict[str, Any]) -> datetime | None:
    """Parse the last-updated timestamp from a Metabase card."""
    raw = card.get("updated_at") or card.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_official(card: dict[str, Any]) -> bool:
    """Return True when the question is in an official/verified collection."""
    collection = card.get("collection") or {}
    return bool(collection.get("authority_level") == "official")


def _assign_role(card: dict[str, Any]) -> UsageRole:
    """Determine UsageRole from the card's trust signal and view count.

    Only promotes to ``trusted_example`` when the question is both official (in an
    official/authority collection) and frequently run.  All other questions are
    ``alternative`` — the conservative, invariant-safe default.
    """
    if _is_official(card) and int(card.get("view_count", 0)) >= _TRUSTED_EXAMPLE_MIN_VIEWS:
        return UsageRole.TRUSTED_EXAMPLE
    return UsageRole.ALTERNATIVE


@runtime_checkable
class MetabaseQuestionSource(Protocol):
    """DI seam for fetching raw Metabase question (card) objects."""

    async def list_questions(self) -> list[dict[str, Any]]: ...

    async def server_version(self) -> str: ...


class HttpMetabaseQuestionSource:
    """Default question source that calls the live Metabase REST API via httpx.

    ``httpx`` is imported lazily so the module can be imported without it;
    add ``httpx>=0.27`` to project dependencies before using this class.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

    async def list_questions(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for live Metabase API access; "
                "add httpx>=0.27 to your project dependencies"
            ) from exc

        async with httpx.AsyncClient(headers=self._headers()) as client:
            resp = await client.get(f"{self._base_url}/api/card")
            resp.raise_for_status()
            return list(resp.json())

    async def server_version(self) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for live Metabase API access; "
                "add httpx>=0.27 to your project dependencies"
            ) from exc

        async with httpx.AsyncClient(headers=self._headers()) as client:
            resp = await client.get(f"{self._base_url}/api/session/properties")
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("version", {}).get("tag", ""))


class MetabaseConnector(ConnectorBase):
    """Evidence connector for Metabase questions → normalized UsageEvidence (SPEC-E3 §3.3, §5).

    Questions are extracted as ``UsageEvidence`` with role ``alternative`` or
    ``trusted_example`` — never ``canonical``.  The role is determined deterministically
    from the question's collection authority level and view count (no LLM).

    Args:
        connection: Canon connection config supplying ``params.base_url`` and
            ``credentials_ref`` (the Metabase API key).
        question_source: Injectable source for testing.  When ``None`` an
            :class:`HttpMetabaseQuestionSource` is built from the connection config.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        question_source: MetabaseQuestionSource | None = None,
    ) -> None:
        from canon.credentials import resolve_credential

        self._source = connection.id
        params = connection.params
        self._base_url: str = params.get("base_url", "")

        if question_source is not None:
            self._question_source: MetabaseQuestionSource = question_source
        else:
            api_key = resolve_credential(connection.credentials_ref)
            self._question_source = HttpMetabaseQuestionSource(self._base_url, api_key)

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION, Capability.EXTRACT_EVIDENCE]

    async def test_connection(self) -> Health:
        """Verify API connectivity and validate the server version is ≥ 0.48."""
        try:
            version_tag = await self._question_source.server_version()
        except Exception as exc:
            return Health(status="error", message=f"Metabase API unreachable: {exc}")

        parsed = _parse_version(version_tag)
        if parsed is None or parsed < MIN_API_VERSION:
            min_str = ".".join(str(v) for v in MIN_API_VERSION)
            return Health(
                status="error",
                message=(
                    f"Metabase server version {version_tag!r} is below minimum {min_str}; "
                    "upgrade Metabase or pin a supported version"
                ),
            )
        return Health(status="ok", message=f"Metabase {version_tag}")

    async def extract_evidence(self) -> list[DocEvidence | UsageEvidence]:
        """Fetch Metabase questions and return one UsageEvidence per question.

        Every question is emitted — none are dropped.  Unmappable expressions are
        recorded as ``unknown`` with a WARNING so the evidence stream is complete
        (SPEC-E3 §4, S2 AC2 pattern).
        """
        observed_at = datetime.now(UTC)
        questions = await self._question_source.list_questions()
        evidence: list[DocEvidence | UsageEvidence] = []

        for card in questions:
            card_id = card.get("id", "")
            title = card.get("name", "") or ""
            artifact = f"question:{card_id}"
            expr = _extract_expr(card)
            references = _extract_references(card)
            role = _assign_role(card)
            frequency = int(card.get("view_count", 0))
            last_seen = _extract_last_seen(card)
            native_ref = f"metabase:question:{card_id}"
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
