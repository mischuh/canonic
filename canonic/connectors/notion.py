"""Notion evidence connector — pages → normalized DocEvidence (SPEC-E3 §5, §9 S2).

Composed from the generic fetch/extract split (:mod:`canonic.connectors.evidence`,
specs/AMENDMENT-generic-evidence-connector.md): :class:`NotionFetchAdapter` fetches
Notion pages via the Notion API (auth, pagination, no classification), and
:class:`NotionExtractionSkill` maps each :class:`~canonic.connectors.evidence.RawDoc`
to :class:`DocEvidence`. ``usage_hint`` is read from an explicit page select property
(deterministic, no LLM — SPEC-E3 §10); ``topic_refs`` come from a multi-select/relation
property and are *candidates* only (resolution is E4/E6's job on write, §5).
:func:`make_notion_connector` wires both into a :class:`GenericEvidenceConnector`.

Version pinning: the ``Notion-Version`` header is pinned to a specific API version per
the compatibility matrix (§6).  On an unsupported or unknown API version the fetch
adapter fails with a clear error at ``test_connection``/extract time and ingests
nothing from that source — partial ingest is never silently accepted (PRD FR-2).

HTTP fetching uses a dependency-injection seam (:class:`NotionPageSource`) so the
connector can be tested without network access.  The default implementation
(:class:`HttpNotionPageSource`) uses ``httpx`` imported lazily; add ``httpx>=0.27``
to project dependencies to use live API access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from canonic.connectors.base import DocEvidence, UsageHint
from canonic.connectors.evidence import (
    GenericEvidenceConnector,
    RawDoc,
    compute_doc_fingerprint,
)
from canonic.connectors.evidence import (
    parse_usage_hint as _usage_hint_for,
)
from canonic.exc import UnsupportedSourceVersionError

__all__ = [
    "HttpNotionPageSource",
    "NotionExtractionSkill",
    "NotionFetchAdapter",
    "NotionPageSource",
    "make_notion_connector",
]

# Pinned API version per SPEC-E3 §6 compatibility matrix.
DEFAULT_API_VERSION = "2022-06-28"

# Only explicitly supported versions are accepted; an unknown version yields a clear error.
SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({DEFAULT_API_VERSION})

# Notion page property name that carries the usage hint (a select property).
_USAGE_HINT_PROPERTY = "Canonic Type"

# Notion page property name that carries topic refs (a multi-select property).
_TOPIC_REFS_PROPERTY = "Canonic Topics"


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
    it stores the result in the ``_body`` key (a Canonic-internal convention).
    """
    return str(page.get("_body", ""))


def _extract_usage_hint(page: dict[str, Any]) -> UsageHint:
    """Read the Canonic Type select property and map it to UsageHint."""
    page_id: str = page.get("id", "")
    props = page.get("properties", {})
    hint_prop = props.get(_USAGE_HINT_PROPERTY, {})
    select = hint_prop.get("select") or {}
    raw = select.get("name")
    return _usage_hint_for(raw, page_id)


def _extract_topic_refs(page: dict[str, Any]) -> list[str]:
    """Read the Canonic Topics multi-select property and return the option names."""
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


class NotionFetchAdapter:
    """Fetch adapter for Notion pages — auth, pagination, version pinning (no classification).

    Args:
        token: Notion integration token (required when ``page_source`` is None).
        api_version: Notion API version header.  Must be in ``SUPPORTED_API_VERSIONS``.
        page_source: Injectable page-source for testing.  When ``None`` an
            :class:`HttpNotionPageSource` is built from ``token``/``api_version``.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_version: str = DEFAULT_API_VERSION,
        page_source: NotionPageSource | None = None,
    ) -> None:
        self._api_version = api_version
        if page_source is not None:
            self._page_source: NotionPageSource = page_source
        else:
            if not token:
                raise ValueError("token is required when page_source is not provided")
            self._page_source = HttpNotionPageSource(token, api_version)

    def _assert_supported_version(self) -> None:
        """Enforce the pinned API-version allowlist; raise if out of range."""
        if self._api_version not in SUPPORTED_API_VERSIONS:
            raise UnsupportedSourceVersionError(
                "Notion API",
                detected=self._api_version,
                supported=", ".join(sorted(SUPPORTED_API_VERSIONS)),
            )

    async def fetch(self) -> list[RawDoc]:
        """Fetch Notion pages and return one RawDoc per page.

        Fails with :exc:`UnsupportedSourceVersionError` on an unsupported API version,
        before any network call, so no partial ingest occurs (SPEC-E3 §6, PRD FR-2).
        Each page's native properties/body are passed through as ``metadata`` for the
        extraction skill to classify (structured select/multi-select properties, in
        Notion's case, rather than free text).
        """
        self._assert_supported_version()
        pages = await self._page_source.list_pages()
        return [
            RawDoc(
                source_ref=f"notion:page:{page.get('id', '')}",
                title=_extract_title(page),
                body=_extract_body(page),
                metadata=page,
            )
            for page in pages
        ]


class NotionExtractionSkill:
    """Deterministic extraction: reads the ``Canonic Type``/``Canonic Topics`` select properties.

    Notion pages already carry an explicit classification (unlike free-text prose
    sources), so no LLM call is needed here — this reproduces the connector's
    pre-split behavior exactly, now expressed as an
    :class:`~canonic.connectors.evidence.ExtractionSkill` (SPEC-E3 §10).  ``usage_hint``
    comes from the ``Canonic Type`` select property; ``topic_refs`` come from the
    ``Canonic Topics`` multi-select property and are candidates only — E6 resolves them
    against live semantic entities on write (§5, §3.1).
    """

    async def extract(self, doc: RawDoc, *, source: str) -> DocEvidence:
        page = doc.metadata
        usage_hint = _extract_usage_hint(page)
        topic_refs = _extract_topic_refs(page)
        fingerprint = compute_doc_fingerprint(doc.title, doc.body, usage_hint.value, topic_refs)
        return DocEvidence(
            source=source,
            title=doc.title,
            body=doc.body,
            topic_refs=topic_refs,
            usage_hint=usage_hint,
            native_ref=doc.source_ref,
            source_fingerprint=fingerprint,
            observed_at=datetime.now(UTC),
        )


def make_notion_connector(
    token: str | None = None,
    *,
    source: str = "notion_wiki",
    api_version: str = DEFAULT_API_VERSION,
    page_source: NotionPageSource | None = None,
) -> GenericEvidenceConnector:
    """Build the Notion evidence connector: ``NotionFetchAdapter`` + deterministic extraction.

    Replaces the pre-split monolithic ``NotionConnector`` with no change to the
    registered factory type name (``"notion"``) or external behavior (SPEC-E3 §5
    fetch/extract-split amendment).
    """
    return GenericEvidenceConnector(
        NotionFetchAdapter(token, api_version=api_version, page_source=page_source),
        source=source,
        extraction_skill=NotionExtractionSkill(),
    )
