"""Knowledge page schema — the Pydantic model tree for knowledge/**/*.md (SPEC-E6 §2).

A page is Markdown with YAML frontmatter. Path determines ``scope`` and ``id``;
neither is hand-set in the frontmatter (the loader derives them — SPEC-E6 §2).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime
from enum import StrEnum
from pathlib import Path  # noqa: TC003 — Pydantic resolves annotations at runtime

from pydantic import BaseModel, ConfigDict

from canonic.semantic.models import Provenance

__all__ = [
    "KnowledgePage",
    "KnowledgePageMeta",
    "KnowledgeScope",
    "KnowledgeValidationError",
    "UsageMode",
]


class KnowledgeValidationError(ValueError):
    """A cross-field validation failure that carries the frontmatter path it concerns.

    Subclasses ValueError so Pydantic wraps it into a ValidationError on direct
    construction; the loader recovers ``path`` (via the error's ctx) to resolve a
    precise file+line for the message. Mirrors ``SemanticValidationError``.
    """

    def __init__(self, path: tuple[str | int, ...], message: str) -> None:
        self.path = path
        super().__init__(message)


class UsageMode(StrEnum):
    """How a page participates in retrieval beyond plain search (SPEC-E6 §8)."""

    REFERENCE = "reference"  # found by search/traversal only; the default
    CAVEAT = "caveat"  # auto-surfaced when a bound sl_ref entity appears in a result
    POLICY = "policy"  # a business rule/definition page; tagged distinguishably
    DEFINITION = "definition"  # canonical prose definition for a term


class KnowledgeScope(StrEnum):
    """Path-defined visibility scope of a page (SPEC-E6 §4)."""

    GLOBAL = "global"  # knowledge/global/ — shared, authoritative
    USER = "user"  # knowledge/user/<id>/ — personal, strictly additive


class KnowledgePageMeta(BaseModel):
    """System-managed page metadata, not hand-edited (SPEC-E6 §2)."""

    model_config = ConfigDict(frozen=True)

    provenance: Provenance = Provenance.INFERRED
    # When the page's references were last checked against live entities (§7 freshness).
    last_validated_at: datetime | None = None
    # Measure-definition fingerprints this page depends on, keyed by fully-qualified
    # entity name (§7 drift): e.g. {"warehouse_pg.orders.total_revenue": "sha256:…"}.
    bound_fingerprints: dict[str, str] = {}
    # Human-owned freeze marker E4 reads: reconciliation flags but never edits (E4 §5.3).
    frozen: bool = False


class KnowledgePage(BaseModel):
    """One committed knowledge page (SPEC-E6 §2).

    ``id``, ``path``, and ``scope`` are derived from the filesystem path by the
    loader, never accepted from frontmatter. Every other field is optional with a
    sensible default, so a page with empty frontmatter is valid.
    """

    model_config = ConfigDict(frozen=True)

    id: str  # slug derived from filename; unique within scope
    path: Path  # source path on disk
    scope: KnowledgeScope  # derived from path (knowledge/global vs knowledge/user/<id>)
    summary: str = ""  # one line; indexed + shown in results
    tags: list[str] = []
    sl_refs: list[str] = []  # → semantic entities (E5), fully qualified
    refs: list[str] = []  # → other pages by id/slug
    usage_mode: UsageMode = UsageMode.REFERENCE
    meta: KnowledgePageMeta = KnowledgePageMeta()
    body: str = ""  # Markdown body, verbatim
