"""Report schema — the Pydantic model tree for reports/*.yaml (AMENDMENT-curated-reports).

A report names an ordered sequence of already-existing calls (``core.query``, optionally
``core.read_page``); it introduces no new execution semantics and no new authority (PRD §5.1
split unchanged). ``ReportSection.query`` is a :class:`~canonic.compiler.query.SemanticQuery` —
the same shape a ``query()`` call or a ``-f``/``--file`` JSON query file accepts — so a report
section's grammar is not a second, parallel one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from canonic.compiler.query import (
    SemanticQuery,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)

__all__ = ["Report", "ReportSection"]


class ReportSection(BaseModel):
    """One named query within a report, with an optional attached narrative."""

    model_config = ConfigDict(frozen=True)

    title: str
    query: SemanticQuery
    narrative_from: str | None = None  # optional knowledge-page id (E6 §2)


class Report(BaseModel):
    """A committed, named, ordered sequence of sections (reports/*.yaml)."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: str | None = None
    owner: str | None = None  # free text, informational only
    domain: str | None = None  # optional filter key for list_reports(domain=...)
    context: str | None = None  # reuses the existing guardrail context tag (SPEC-E5-E15 §2.3)
    sections: list[ReportSection] = Field(min_length=1)
