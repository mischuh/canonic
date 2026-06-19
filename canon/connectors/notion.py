"""Notion evidence connector — pages → normalized DocEvidence (SPEC-E3 §5, §9 S2).

Fetches Notion pages via the Notion API and emits one :class:`DocEvidence` per page.
``usage_hint`` is read from an explicit page select property (deterministic, no LLM —
SPEC-E3 §10); ``topic_refs`` come from a multi-select/relation property and are
*candidates* only (resolution is E4/E6's job on write, §5).

Version pinning: the ``Notion-Version`` header is pinned to a specific API version per
the compatibility matrix (§6).  On an unsupported or unknown API version the connector
fails with a clear error at ``test_connection``/extract time and ingests nothing from
that source — partial ingest is never silently accepted (PRD FR-2).

HTTP fetching uses a dependency-injection seam (:class:`NotionPageSource`) so the
connector can be tested without network access.  The default implementation
(:class:`HttpNotionPageSource`) uses ``httpx`` imported lazily; add ``httpx>=0.27``
to project dependencies to use live API access.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from canon.connectors.base import (
    Capability,
    ConnectorBase,
    DocEvidence,
    Health,
    UsageEvidence,
    UsageHint,
)
from canon.exc import UnsupportedSourceVersionError

logger = logging.getLogger(__name__)

__all__ = ["NotionConnector", "NotionPageSource", "HttpNotionPageSource"]

# Pinned API version per SPEC-E3 §6 compatibility matrix.
DEFAULT_API_VERSION = "2022-06-28"

# Only explicitly supported versions are accepted; an unknown version yields a clear error.
SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({DEFAULT_API_VERSION})

# Notion page property name that carries the usage hint (a select property).
_USAGE_HINT_PROPERTY = "Canon Type"

# Notion page property name that carries topic refs (a multi-select property).
_TOPIC_REFS_PROPERTY = "Canon Topics"

# Map from Notion select option values (case-insensitive) → UsageHint.
_USAGE_HINT_MAP: dict[str, UsageHint] = {
    "reference": UsageHint.REFERENCE,
    "caveat": UsageHint.CAVEAT,
    "policy": UsageHint.POLICY,
    "definition": UsageHint.DEFINITION,
}


def _doc_fingerprint(title: str, body: str, usage_hint: str, topic_refs: list[str]) -> str:
    """Stable sha256 over doc content fields, for drift detection (SPEC-E3 §3.2)."""
    payload = {
        "title": title,
        "body": body,
        "usage_hint": usage_hint,
        "topic_refs": sorted(topic_refs),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


def _usage_hint_for(raw: str | None, page_id: str) -> UsageHint:
    """Map a raw Notion select value to UsageHint.

    Defaults to ``REFERENCE`` and emits a WARNING for unrecognized values —
    never drops the page (SPEC-E3 §4 AC2-style graceful handling).
    """
    if not raw:
        return UsageHint.REFERENCE
    mapped = _USAGE_HINT_MAP.get(raw.strip().lower())
    if mapped is None:
        logger.warning(
            "unrecognized Canon Type %r on Notion page %s; usage_hint recorded as reference",
            raw,
            page_id,
        )
        return UsageHint.REFERENCE
    return mapped


def _extract_title(page: dict[str, Any]) -> str:
    """Extract the plain-text title from a Notion page object."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            title_parts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in title_parts)
    return ""


def _extract_body(page: dict[str, Any]) -> str:
    """Extract pre-rendered body text from a Notion page object.

    The page source is responsible for fetching and rendering block content;
    it stores the result in the ``_body`` key (a Canon-internal convention).
    """
    return str(page.get("_body", ""))


def _extract_usage_hint(page: dict[str, Any]) -> UsageHint:
    """Read the Canon Type select property and map it to UsageHint."""
    page_id: str = page.get("id", "")
    props = page.get("properties", {})
    hint_prop = props.get(_USAGE_HINT_PROPERTY, {})
    select = hint_prop.get("select") or {}
    raw = select.get("name")
    return _usage_hint_for(raw, page_id)


def _extract_topic_refs(page: dict[str, Any]) -> list[str]:
    """Read the Canon Topics multi-select property and return the option names."""
    props = page.get("properties", {})
    topics_prop = props.get(_TOPIC_REFS_PROPERTY, {})
    multi_select = topics_prop.get("multi_select", [])
    return [opt.get("name", "") for opt in multi_select if opt.get("name")]


@runtime_checkable
class NotionPageSource(Protocol):
    """DI seam for fetching raw Notion page objects.

    Implementations must return a list of page dicts, each conforming to the
    Notion API page object shape plus an optional ``_body`` key carrying
    pre-rendered block text (populated by the implementation).
    """

    async def list_pages(self) -> list[dict[str, Any]]: ...


class HttpNotionPageSource:
    """Default page source that calls the live Notion API via httpx.

    ``httpx`` is imported lazily so the module can be imported without it;
    add ``httpx>=0.27`` to project dependencies before using this class.
    """

    def __init__(self, token: str, api_version: str = DEFAULT_API_VERSION) -> None:
        self._token = token
        self._api_version = api_version

    async def list_pages(self) -> list[dict[str, Any]]:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for live Notion API access; "
                "add httpx>=0.27 to your project dependencies"
            ) from exc

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._api_version,
            "Content-Type": "application/json",
        }
        pages: list[dict[str, Any]] = []
        start_cursor: str | None = None

        async with httpx.AsyncClient(headers=headers) as client:
            while True:
                body: dict[str, Any] = {"filter": {"value": "page", "property": "object"}}
                if start_cursor:
                    body["start_cursor"] = start_cursor

                resp = await client.post(
                    "https://api.notion.com/v1/search",
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()

                for page in data.get("results", []):
                    page["_body"] = await self._fetch_body(client, page.get("id", ""))
                    pages.append(page)

                if not data.get("has_more"):
                    break
                start_cursor = data.get("next_cursor")

        return pages

    async def _fetch_body(self, client: Any, page_id: str) -> str:
        """Fetch and flatten block children into plain text."""
        resp = await client.get(f"https://api.notion.com/v1/blocks/{page_id}/children")
        if resp.status_code != 200:
            return ""
        blocks = resp.json().get("results", [])
        parts: list[str] = []
        for block in blocks:
            text = _block_text(block)
            if text:
                parts.append(text)
        return "\n".join(parts)


def _block_text(block: dict[str, Any]) -> str:
    """Extract plain text from a single Notion block."""
    btype = block.get("type", "")
    block_data = block.get(btype, {})
    rich_text = block_data.get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in rich_text)


class NotionConnector(ConnectorBase):
    """Evidence connector for Notion pages → normalized DocEvidence (SPEC-E3 §5, §9 S2).

    ``usage_hint`` is read from the ``Canon Type`` select property on each page
    (deterministic — no LLM).  ``topic_refs`` come from the ``Canon Topics``
    multi-select property and are candidates only; E6 resolves them against live
    semantic entities on write (§5, §3.1).

    Args:
        token: Notion integration token (required when ``page_source`` is None).
        source: Connection id used to stamp evidence items (default ``"notion_wiki"``).
        api_version: Notion API version header.  Must be in ``SUPPORTED_API_VERSIONS``.
        page_source: Injectable page-source for testing.  When ``None`` an
            :class:`HttpNotionPageSource` is built from ``token``/``api_version``.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        source: str = "notion_wiki",
        api_version: str = DEFAULT_API_VERSION,
        page_source: NotionPageSource | None = None,
    ) -> None:
        self._source = source
        self._api_version = api_version
        if page_source is not None:
            self._page_source: NotionPageSource = page_source
        else:
            if not token:
                raise ValueError("token is required when page_source is not provided")
            self._page_source = HttpNotionPageSource(token, api_version)

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION, Capability.EXTRACT_EVIDENCE]

    def _assert_supported_version(self) -> None:
        """Enforce the pinned API-version allowlist; raise if out of range."""
        if self._api_version not in SUPPORTED_API_VERSIONS:
            raise UnsupportedSourceVersionError(
                "Notion API",
                detected=self._api_version,
                supported=", ".join(sorted(SUPPORTED_API_VERSIONS)),
            )

    async def test_connection(self) -> Health:
        """Verify the configured API version is supported and the page source is reachable."""
        try:
            self._assert_supported_version()
        except UnsupportedSourceVersionError as exc:
            return Health(status="error", message=str(exc))
        try:
            await self._page_source.list_pages()
        except Exception as exc:
            return Health(status="error", message=f"Notion API unreachable: {exc}")
        return Health(status="ok", message=f"Notion API {self._api_version}")

    async def extract_evidence(self) -> list[DocEvidence | UsageEvidence]:
        """Fetch Notion pages and return one DocEvidence per page.

        Fails with :exc:`UnsupportedSourceVersionError` on an unsupported API version
        so no partial ingest occurs (SPEC-E3 §6, PRD FR-2).
        """
        self._assert_supported_version()

        observed_at = datetime.now(UTC)
        pages = await self._page_source.list_pages()
        evidence: list[DocEvidence | UsageEvidence] = []

        for page in pages:
            page_id: str = page.get("id", "")
            title = _extract_title(page)
            body = _extract_body(page)
            usage_hint = _extract_usage_hint(page)
            topic_refs = _extract_topic_refs(page)
            fingerprint = _doc_fingerprint(title, body, usage_hint.value, topic_refs)

            evidence.append(
                DocEvidence(
                    source=self._source,
                    title=title,
                    body=body,
                    topic_refs=topic_refs,
                    usage_hint=usage_hint,
                    native_ref=f"notion:page:{page_id}",
                    source_fingerprint=fingerprint,
                    observed_at=observed_at,
                )
            )

        return evidence
