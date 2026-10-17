#!/usr/bin/env python3
"""Refresh ``meta.last_validated_at`` on the knowledge pages of every example project.

A page is validated when its ``sl_refs``, ``refs`` and wikilinks resolve against the
project's semantic sources, which is the same check ``canonic ingest`` applies before it
stamps a page. The script refuses to stamp anything when a project's contracts or any page's
references are broken, so a stamp never hides real drift. Only the date line is rewritten,
every other byte of a page stays as authored.

Run from anywhere: ``python scripts/refresh_example_stamps.py``. Exits 1 on drift.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from canonic.contracts.validate import validate_contracts
from canonic.exc import CanonicError
from canonic.knowledge.loader import load_knowledge_page
from canonic.knowledge.validation import EntityIndex, PageIndex, ReferenceValidator
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from canonic.knowledge.models import KnowledgePage

EXAMPLES = Path(__file__).parents[1] / "examples"

#: A page stamped more recently than this is left alone, so a monthly run does not rewrite
#: pages that were just refreshed by hand or by ``canonic ingest``.
RESTAMP_AFTER = timedelta(days=21)

_STAMP_LINE = re.compile(r"^(?P<indent>[ \t]*)last_validated_at:.*$", re.MULTILINE)


def _frontmatter_end(text: str) -> int:
    """Index of the closing ``---`` line of the frontmatter block, or -1 when there is none."""
    if not text.startswith("---\n"):
        return -1
    return text.find("\n---", 4)


def stamp_page(path: Path, stamp: str) -> bool:
    """Set the ``last_validated_at`` line inside the frontmatter of ``path``.

    Returns False when the page has no such line, so the caller can report it instead of
    guessing where the line belongs.
    """
    text = path.read_text()
    end = _frontmatter_end(text)
    if end == -1:
        return False
    head, tail = text[:end], text[end:]
    updated, count = _STAMP_LINE.subn(rf'\g<indent>last_validated_at: "{stamp}"', head, count=1)
    if count == 0:
        return False
    path.write_text(updated + tail)
    return True


def _needs_stamp(page: KnowledgePage, now: datetime) -> bool:
    last = page.meta.last_validated_at
    return last is None or now - last > RESTAMP_AFTER


def check_project(project: Path) -> tuple[list[KnowledgePage], list[str]]:
    """Load ``project``'s pages and collect every drift finding, never raising."""
    findings: list[str] = []
    try:
        validate_contracts(project)
    except CanonicError as exc:
        findings.append(f"{project.name}: contracts: {exc}")
    sources = list_semantic_sources(project)
    pages = [load_knowledge_page(p) for p in sorted((project / "knowledge").rglob("*.md"))]
    validator = ReferenceValidator(EntityIndex.from_sources(sources), PageIndex.from_pages(pages))
    for page in pages:
        try:
            validator.validate_page(page)
        except CanonicError as exc:
            findings.append(f"{project.name}: {exc}")
    return pages, findings


def main() -> int:
    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT00:00:00Z")
    projects = sorted(p for p in EXAMPLES.iterdir() if (p / "canonic.yaml").is_file())
    checked = {project: check_project(project) for project in projects}

    findings = [line for _, found in checked.values() for line in found]
    if findings:
        print("examples have drifted, nothing was stamped:", file=sys.stderr)
        for line in findings:
            print(f"  {line}", file=sys.stderr)
        return 1

    stamped = 0
    for project, (pages, _) in checked.items():
        for page in pages:
            if page.meta.frozen or not _needs_stamp(page, now):
                continue
            if stamp_page(page.path, stamp):
                stamped += 1
            else:
                print(f"no last_validated_at line in {page.path}, skipped", file=sys.stderr)
        print(f"{project.name}: {len(pages)} pages checked")
    print(f"stamped {stamped} pages with {stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
