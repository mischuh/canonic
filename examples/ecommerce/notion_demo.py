"""Offline demo: turn `docs/notion-pages/*.md` into real `DocEvidence` — no workspace, no token.

`docs/notion-pages/` shows the page format the Notion connector expects (the two page
properties it reads deterministically: "Canonic Type" -> usage_hint, "Canonic Topics" ->
topic_refs), but as static markdown those files aren't wired into any ingestion path —
`canonic knowledge add` only fetches `--type url` over HTTP, and the live Notion connector
only speaks the Notion API. Neither can read a local file.

`NotionFetchAdapter`/`NotionExtractionSkill` (canonic/connectors/notion.py) are decoupled
from HTTP entirely: they depend on a `NotionPageSource` protocol, the same seam
`tests/connectors/test_notion.py` uses to test without network access. This script implements
that protocol by reading the markdown files here and shaping them like Notion API page
objects, then runs them through the exact connector code a live workspace would use.

Run from `examples/ecommerce/`:

    python notion_demo.py
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from canonic.connectors.base import DocEvidence
from canonic.connectors.notion import make_notion_connector

PAGES_DIR = Path(__file__).parent / "docs" / "notion-pages"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n(.*)$", re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_YAML = YAML(typ="safe")


@dataclass(frozen=True)
class _ParsedPage:
    title: str
    body: str
    canonic_type: str
    canonic_topics: list[str]


def _parse_page(path: Path) -> _ParsedPage:
    """Split a sample page into frontmatter (`canonic_type`/`canonic_topics`) + title + body.

    Mirrors what a human would configure via the "Canonic Type"/"Canonic Topics" properties
    in the Notion sidebar; the frontmatter's `#`-prefixed lines document that mapping and are
    plain YAML comments, ignored by the parser.
    """
    match = _FRONTMATTER_RE.match(path.read_text())
    if match is None:
        raise ValueError(f"{path} has no --- frontmatter block")
    frontmatter = _YAML.load(match.group(1))
    rest = match.group(2).strip()

    heading = _HEADING_RE.search(rest)
    title = heading.group(1).strip() if heading else path.stem
    body = rest[heading.end() :].strip() if heading else rest

    return _ParsedPage(
        title=title,
        body=body,
        canonic_type=frontmatter["canonic_type"],
        canonic_topics=list(frontmatter["canonic_topics"]),
    )


def _as_notion_page(page: _ParsedPage, *, page_id: str) -> dict[str, Any]:
    """Shape a parsed page as a Notion API page object (SPEC-E3 §5): the exact fields
    `NotionFetchAdapter`/`NotionExtractionSkill` read — a title property, the "Canonic Type"
    select, the "Canonic Topics" multi-select, and a pre-rendered `_body`.
    """
    return {
        "id": page_id,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": page.title}]},
            "Canonic Type": {"select": {"name": page.canonic_type}},
            "Canonic Topics": {"multi_select": [{"name": topic} for topic in page.canonic_topics]},
        },
        "_body": page.body,
    }


class LocalMarkdownNotionPageSource:
    """`NotionPageSource` backed by local markdown instead of the live Notion API.

    Implements the same protocol `HttpNotionPageSource` does (canonic/connectors/notion.py),
    so `NotionFetchAdapter` and `NotionExtractionSkill` run unmodified against it.
    """

    def __init__(self, pages_dir: Path) -> None:
        self._pages_dir = pages_dir

    async def list_pages(self) -> list[dict[str, Any]]:
        paths = sorted(self._pages_dir.glob("*.md"))
        return [_as_notion_page(_parse_page(path), page_id=f"local:{path.stem}") for path in paths]


async def main() -> None:
    connector = make_notion_connector(
        source="handbook_notion",
        page_source=LocalMarkdownNotionPageSource(PAGES_DIR),
    )
    evidence = await connector.extract_evidence()

    for doc in evidence:
        assert isinstance(doc, DocEvidence)  # NotionExtractionSkill only ever emits DocEvidence
        print(f"{doc.title!r}")
        print(f"  usage_hint:  {doc.usage_hint.value}")
        print(f"  topic_refs:  {doc.topic_refs}")
        print(f"  native_ref:  {doc.native_ref}")
        print(f"  fingerprint: {doc.source_fingerprint}")
        print()

    print(f"{len(evidence)} DocEvidence record(s) extracted from {PAGES_DIR}.")
    print("Compare usage_hint/topic_refs above to usage_mode/tags in knowledge/global/*.md —")
    print("that's what canonic ingest would write from a live Notion workspace.")


if __name__ == "__main__":
    asyncio.run(main())
