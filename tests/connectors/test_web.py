"""Tests for UrlFetchAdapter (SPEC-E3 §5 fetch/extract-split amendment).

Uses the ``page_source`` DI seam (mirrors NotionPageSource/FixtureNotionPageSource) so
these tests need no network access and no ``httpx`` installed.
"""

from __future__ import annotations

import pytest

from canonic.connectors.web import UrlFetchAdapter


class FixtureUrlPageSource:
    """In-process page source returning canned HTML per URL, no network access."""

    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages

    async def fetch(self, url: str) -> str:
        return self._pages[url]


class TestUrlFetchAdapter:
    async def test_one_url_produces_one_raw_doc(self) -> None:
        source = FixtureUrlPageSource(
            {
                "https://example.com/kpis": "<html><title>KPIs</title><body>MRR is revenue.</body></html>"
            }
        )
        adapter = UrlFetchAdapter(["https://example.com/kpis"], page_source=source)

        docs = await adapter.fetch()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.source_ref == "web:https://example.com/kpis"
        assert doc.title == "KPIs"
        assert "MRR is revenue." in doc.body
        assert doc.metadata == {"url": "https://example.com/kpis"}

    async def test_multiple_urls_produce_docs_in_order(self) -> None:
        source = FixtureUrlPageSource(
            {
                "https://a.example.com": "<title>A</title><body>Page A</body>",
                "https://b.example.com": "<title>B</title><body>Page B</body>",
            }
        )
        adapter = UrlFetchAdapter(
            ["https://a.example.com", "https://b.example.com"], page_source=source
        )

        docs = await adapter.fetch()

        assert [d.title for d in docs] == ["A", "B"]
        assert [d.source_ref for d in docs] == [
            "web:https://a.example.com",
            "web:https://b.example.com",
        ]

    async def test_missing_title_falls_back_to_url(self) -> None:
        source = FixtureUrlPageSource({"https://example.com/no-title": "<body>Just prose.</body>"})
        adapter = UrlFetchAdapter(["https://example.com/no-title"], page_source=source)

        docs = await adapter.fetch()

        assert docs[0].title == "https://example.com/no-title"

    async def test_script_and_style_tags_are_stripped_from_body(self) -> None:
        html = (
            "<html><head><style>.x{color:red}</style></head>"
            "<body><script>var x = 1;</script><p>Real content.</p></body></html>"
        )
        source = FixtureUrlPageSource({"https://example.com/x": html})
        adapter = UrlFetchAdapter(["https://example.com/x"], page_source=source)

        docs = await adapter.fetch()

        assert "color:red" not in docs[0].body
        assert "var x = 1" not in docs[0].body
        assert "Real content." in docs[0].body

    async def test_fetch_failure_propagates_uncaught(self) -> None:
        class _FailingSource:
            async def fetch(self, url: str) -> str:
                raise RuntimeError("connection refused")

        adapter = UrlFetchAdapter(["https://example.com/down"], page_source=_FailingSource())

        with pytest.raises(RuntimeError, match="connection refused"):
            await adapter.fetch()
