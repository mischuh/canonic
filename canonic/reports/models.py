"""Report schema — the Pydantic model tree for reports/**/*.yaml.

A report names an ordered sequence of already-existing calls (``core.query``, optionally
``core.read_page``); it introduces no new execution semantics and no new authority (PRD §5.1
split unchanged). ``ReportSection.query`` is a :class:`~canonic.compiler.query.SemanticQuery` —
the same shape a ``query()`` call or a ``-f``/``--file`` JSON query file accepts — so a report
section's grammar is not a second, parallel one.

``ReportSection.id`` and ``Report.question`` are additive fields introduced by
AMENDMENT-user-scoped-queries-reports: ``id`` makes a section addressable for
``update_report(remove_sections=...)``, and ``question`` carries the plain-language phrasing a
``save_query`` call records for ``get_overview``'s ``sample_questions``. Both are optional so
hand-authored ``global/`` files that predate this amendment stay valid unchanged.
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
    id: str | None = None  # stable section id; set by compose_report/update_report (S25)


class Report(BaseModel):
    """A committed, named, ordered sequence of sections (reports/**/*.yaml)."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: str | None = None
    owner: str | None = None  # free text, informational only
    domain: str | None = None  # optional filter key for list_reports(domain=...)
    context: str | None = None  # reuses the existing guardrail context tag (SPEC-E5-E15 §2.3)
    question: str | None = None  # plain-language phrasing; feeds get_overview (S24 AC3)
    sections: list[ReportSection] = Field(min_length=1)
