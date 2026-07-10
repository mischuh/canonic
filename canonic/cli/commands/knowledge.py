"""``canonic knowledge`` — knowledge search (E6, P1) and one-shot page authoring.

``add`` is the one-shot counterpart to the recurring ``canonic ingest`` path (SPEC-E3 §5
fetch/extract-split amendment): fetch a single external doc, classify it via the same
``ExtractionSkill`` seam evidence connectors use, preview the resulting knowledge page,
and write it after confirmation — with no ``canonic.yaml`` connection required. Knowledge
pages are loaded straight from ``knowledge/**/*.md`` (``canonic/knowledge/loader.py``),
independent of connectors/ingest, so this command never touches the ``ConnectorFactory``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import _console, load_service
from canonic.config import find_project_root, load_config
from canonic.connectors.web import UrlFetchAdapter
from canonic.exc import KnowledgePageError, UnknownConnectorType
from canonic.knowledge.loader import dump_knowledge_page
from canonic.knowledge.models import KnowledgePageMeta, KnowledgeScope, UsageMode
from canonic.knowledge.resolve import resolve_topic_refs
from canonic.knowledge.validation import EntityIndex, PageIndex, ReferenceValidator
from canonic.runtime.extraction import make_extraction_skill
from canonic.semantic.loader import list_semantic_sources
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from canonic.config import CanonicConfig
    from canonic.connectors.base import DocEvidence
    from canonic.connectors.evidence import FetchAdapter
    from canonic.knowledge.models import KnowledgePage
    from canonic.knowledge.results import SearchResult
    from canonic.semantic.models import SemanticSource

app = typer.Typer(name="knowledge", help="Search project knowledge and semantics.")

# Ad-hoc fetch-adapter registry for the one-shot `add` path — keyed by --type, takes a bare
# ref string (not a full canonic.yaml Connection). Deliberately separate from
# ConnectorFactory (canonic/connectors/factory.py): that registry builds a ConnectorBase
# from a Connection (capabilities, test_connection, credentials); `add` needs neither a
# connection id nor the capability contract, just fetch() for one reference.
_ADHOC_ADAPTERS: dict[str, Callable[[str], FetchAdapter]] = {
    "url": lambda ref: UrlFetchAdapter([ref]),
}


@app.command("search")
@handle_errors
def search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search text.")],
    user: Annotated[
        str | None, typer.Option("--user", help="Requesting user id, for access control.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max hits to return.")] = 10,
) -> None:
    """Hybrid search over knowledge pages for business context (E6, P1).

    With ``--json`` the output matches the MCP ``search_knowledge`` tool payload byte-for-byte.
    """
    result = load_service(ctx).search_knowledge(query, user=user, limit=limit)
    payload = {
        "hits": [
            {
                "page": h.page,
                "summary": h.summary,
                "usage_mode": h.usage_mode,
                "matched_on": [m.value for m in h.matched_on],
                "sl_refs": h.sl_refs,
            }
            for h in result.hits
        ],
        "caveats": [
            {"page": c.page, "summary": c.summary, "triggered_by": c.triggered_by}
            for c in result.caveats
        ],
    }

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    _render_search(result)


def _render_search(result: SearchResult) -> None:
    """Render ranked hits plus any auto-surfaced caveats as human-readable text."""
    if not result.hits:
        _console.print("[yellow]no hits[/yellow]")
    for h in result.hits:
        arms = "+".join(m.value for m in h.matched_on)
        _console.print(
            f"[bold]{h.page}[/bold]  score={h.score:.3f}  via={arms}  usage={h.usage_mode.value}"
        )
        _console.print(f"  {h.summary}")
        if h.sl_refs:
            _console.print(f"  sl_refs: {', '.join(h.sl_refs)}")
    if result.caveats:
        _console.print("\n[bold yellow]caveats:[/bold yellow]")
        for c in result.caveats:
            _console.print(f"  {c.page}: {c.summary}")


@app.command("add")
@handle_errors
def add(
    ctx: typer.Context,  # noqa: ARG001 — required by handle_errors
    ref: Annotated[str, typer.Argument(help="Source reference to fetch (e.g. a URL).")],
    type_: Annotated[str, typer.Option("--type", help="Ad-hoc fetch adapter type.")] = "url",
    user: Annotated[
        str | None,
        typer.Option("--user", help="Write to knowledge/user/<id>/ instead of knowledge/global/."),
    ] = None,
    slug: Annotated[
        str | None, typer.Option("--slug", help="Override the derived filename slug.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Write without a confirmation prompt.")
    ] = False,
) -> None:
    """Fetch one external doc and write it as a knowledge page, previewed before writing."""
    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canonic project found — run from inside a project directory"
        )
        raise typer.Exit(1)
    config = load_config(root / "canonic.yaml")
    asyncio.run(_add(root, config, ref, type_, user=user, slug=slug, yes=yes))


def _slugify(title: str) -> str:
    """Filesystem-safe slug derived from a title; a short content hash if that's empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or f"doc-{hashlib.sha256(title.encode()).hexdigest()[:8]}"


def _summarize(body: str, *, limit: int = 200) -> str:
    """First ``limit`` chars of ``body``, collapsed to one line, cut at a word boundary."""
    flat = " ".join(body.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit].rsplit(' ', 1)[0]}…"


def _build_page(
    evidence: DocEvidence,
    resolved_sl_refs: list[str],
    entity_index: EntityIndex,
    *,
    project_root: Path,
    user: str | None,
    slug: str | None,
) -> KnowledgePage:
    """Map a classified DocEvidence + resolved sl_refs into a draft KnowledgePage.

    ``summary`` and ``tags`` have no upstream signal to draw on beyond the body itself —
    ``summary`` is a crude body-prefix, not a human-quality one-liner; ``meta.provenance``
    is always INFERRED so downstream readers know this page was never hand-reviewed.
    """
    from canonic.knowledge.models import KnowledgePage

    page_slug = slug or _slugify(evidence.title)
    if user is not None:
        path = project_root / "knowledge" / "user" / user / f"{page_slug}.md"
        scope = KnowledgeScope.USER
    else:
        path = project_root / "knowledge" / "global" / f"{page_slug}.md"
        scope = KnowledgeScope.GLOBAL

    bound_fingerprints = {
        ref: fp
        for ref in resolved_sl_refs
        if (fp := entity_index.current_fingerprint(ref)) is not None
    }

    return KnowledgePage(
        id=page_slug,
        path=path,
        scope=scope,
        summary=_summarize(evidence.body),
        sl_refs=resolved_sl_refs,
        usage_mode=UsageMode(evidence.usage_hint.value),
        meta=KnowledgePageMeta(
            provenance=Provenance.INFERRED,
            last_validated_at=datetime.now(UTC),
            bound_fingerprints=bound_fingerprints,
        ),
        body=evidence.body,
    )


async def _add(
    root: Path,
    config: CanonicConfig,
    ref: str,
    type_: str,
    *,
    user: str | None,
    slug: str | None,
    yes: bool,
) -> None:
    """Fetch, classify, preview, confirm, and write one knowledge page."""
    builder = _ADHOC_ADAPTERS.get(type_)
    if builder is None:
        raise UnknownConnectorType(type_, known=sorted(_ADHOC_ADAPTERS))

    raw_docs = await builder(ref).fetch()
    if len(raw_docs) != 1:
        raise KnowledgePageError(
            f"{type_!r} adapter for {ref!r} returned {len(raw_docs)} document(s); "
            "`canonic knowledge add` writes exactly one page — use a canonic.yaml "
            "connection + `canonic ingest` for multi-document sources"
        )

    extraction_skill = make_extraction_skill(config.llm, config.runtime, headless=False)
    evidence = await extraction_skill.extract(raw_docs[0], source=type_)

    sources: list[SemanticSource] = list_semantic_sources(root)
    resolved, unresolved = resolve_topic_refs(evidence.topic_refs, sources)
    entity_index = EntityIndex.from_sources(sources)

    page = _build_page(evidence, resolved, entity_index, project_root=root, user=user, slug=slug)
    rendered = dump_knowledge_page(page)

    _console.print(rendered)
    if unresolved:
        _console.print(
            f"[yellow]note:[/yellow] {len(unresolved)} topic_ref candidate(s) will NOT be "
            f"linked (no matching semantic entity): {unresolved}"
        )

    if not yes and not typer.confirm(f"Write {page.path}?"):
        _console.print("[yellow]aborted:[/yellow] nothing written")
        raise typer.Exit(0)

    # Safety net: `resolved` was only ever populated from entity_index itself, so this
    # should never raise — cheap insurance against drift between resolution and write.
    ReferenceValidator(entity_index, PageIndex(slugs_by_scope={})).validate_sl_refs(page)

    page.path.parent.mkdir(parents=True, exist_ok=True)
    page.path.write_text(rendered)
    _console.print(f"[green]wrote[/green] {page.path}")
