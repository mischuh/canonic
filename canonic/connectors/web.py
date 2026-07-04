"""URL fetch adapter — plain web pages fetched by HTTP GET (E3 §5 fetch/extract split).

A URL carries no structured classification fields at all (no "Canonic Type" property
like Notion, no issue type like Jira) — this is exactly the case
:class:`~canonic.runtime.extraction.RuntimeExtractionSkill` (LLM-backed) exists for. No
deterministic extraction skill is registered for it: ``GenericEvidenceConnector`` defaults
to ``NullExtractionSkill`` until a real one is backfilled (``ingest.py``'s
``_wire_extraction_skills`` for the recurring path, ``canonic knowledge add`` for the
one-shot path).

HTTP fetching uses a dependency-injection seam (:class:`UrlPageSource`), mirroring
:mod:`canonic.connectors.notion`, so the adapter can be tested without network access or
the optional ``httpx`` dependency installed.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from canonic.connectors.evidence import RawDoc

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["HttpUrlPageSource", "UrlFetchAdapter", "UrlPageSource"]


class _TextExtractor(HTMLParser):
    """Strips tags/script/style, keeping ``<title>`` and a flattened text body."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        stripped = data.strip()
        if stripped:
            self._chunks.append(stripped)

    @property
    def body(self) -> str:
        return "\n".join(self._chunks)


def _html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, body)`` plain text extracted from raw HTML.

    Stdlib-only (``html.parser``) so no new dependency is required — good enough for
    prose pages; a site needing readability-grade extraction (ads/nav stripped, main
    content isolated) should add a dedicated library and adapt this function.
    """
    parser = _TextExtractor()
    parser.feed(html)
    return parser.title.strip(), parser.body


@runtime_checkable
class UrlPageSource(Protocol):
    """DI seam for fetching one URL's raw HTML — no parsing, no extraction."""

    async def fetch(self, url: str) -> str: ...


class HttpUrlPageSource:
    """Default page source that fetches a URL live via ``httpx``.

    ``httpx`` is imported lazily so the module can be imported without it; add
    ``httpx>=0.27`` to project dependencies before using this class.
    """

    async def fetch(self, url: str) -> str:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for URL fetching; add httpx>=0.27 to project dependencies"
            ) from exc

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text


class UrlFetchAdapter:
    """Fetch adapter for a fixed list of URLs — no auth, no pagination, no structure.

    Each URL becomes exactly one :class:`~canonic.connectors.evidence.RawDoc`.

    Args:
        urls: The URLs to fetch, in order.
        page_source: Injectable page-source for testing. When ``None`` a
            :class:`HttpUrlPageSource` is used.
    """

    def __init__(self, urls: Sequence[str], *, page_source: UrlPageSource | None = None) -> None:
        self._urls = list(urls)
        self._page_source: UrlPageSource = page_source or HttpUrlPageSource()

    async def fetch(self) -> list[RawDoc]:
        docs: list[RawDoc] = []
        for url in self._urls:
            html = await self._page_source.fetch(url)
            title, body = _html_to_text(html)
            docs.append(
                RawDoc(
                    source_ref=f"web:{url}",
                    title=title or url,
                    body=body,
                    metadata={"url": url},
                )
            )
        return docs
