"""Contract candidates from definition connectors: proposed metric bindings, never canonical.

A definition connector (Ossie) can state a metric that only a binding expresses: a
``COUNT(DISTINCT …)`` or a ratio of two aggregates. The builder turns each such candidate
into proposed ``contracts/metrics/<slug>.yaml`` files, marked with :data:`CANDIDATE_SENTINEL`.

Reconciliation resolves them after every other proposal, so it knows which measures and
bindings the run will leave in place:

- a binding already exists at the target: no-op, an import never touches a binding;
- every measure, column or component binding it references exists: add, for review;
- otherwise: no-op with the reason, instead of failing the validation gate for the run.

Candidates enter at ``inferred`` with capped confidence, so they never auto-apply and only
become canonical through review (FR-13).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from canonic.connectors.base import AcquisitionTier, CandidateKind
from canonic.ingestion.models import (
    DraftedBy,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
)
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.ingestion.definitions import DefinitionIndex
    from canonic.ingestion.reconciliation import AcceptedStore

logger = logging.getLogger(__name__)

__all__ = [
    "CANDIDATE_SENTINEL",
    "CandidateResolver",
    "build_candidate_proposals",
    "final_sources",
    "is_candidate",
]

#: Marks a proposed binding as a contract candidate. Stripped before anything is written.
CANDIDATE_SENTINEL = "_contract_candidate"

_ADDING = frozenset({ReconciliationDecision.ADD, ReconciliationDecision.EDIT})


def _target(metric: str) -> str:
    """Same slug rule the accepted store keys bindings by, so an existing one is found."""
    return f"contracts/metrics/{metric.replace(' ', '_').lower()}.yaml"


def is_candidate(proposal: Proposal) -> bool:
    return CANDIDATE_SENTINEL in proposal.content


def _distinct_column(expr: str) -> str | None:
    """The column of a ``COUNT(DISTINCT col)`` measure, ``None`` for any other shape."""
    try:
        parsed = sqlglot.parse_one(expr)
    except SqlglotError:
        return None
    if not (isinstance(parsed, exp.Count) and isinstance(parsed.this, exp.Distinct)):
        return None
    (column,) = parsed.this.expressions or (None,)
    return column.name if isinstance(column, exp.Column) else None


def build_candidate_proposals(
    index: DefinitionIndex, *, confidence: float
) -> tuple[list[Proposal], list[tuple[str, str]]]:
    """Turn every contract candidate into binding proposals.

    A ratio also proposes a binding for each of its components, named after the component
    measure, so the ratio has metrics to point at. Returns the proposals, components first,
    and ``(source, reason)`` for every candidate that cannot be expressed.
    """
    proposals: dict[str, Proposal] = {}
    unexpressible: list[tuple[str, str]] = []

    def propose(
        metric: str, canonical: dict[str, Any], aliases: list[str], anchor: str | None
    ) -> None:
        target = _target(metric)
        if target in proposals:
            return
        content: dict[str, Any] = {
            "metric": metric,
            "canonical": canonical,
            "provenance": Provenance.INFERRED.value,
            CANDIDATE_SENTINEL: True,
        }
        aliases = [a for a in aliases if a != metric]
        if aliases:
            content["aliases"] = aliases
        proposals[target] = Proposal(
            target=target,
            op=ProposalOp.ADD,
            content=content,
            provenance=Provenance.INFERRED,
            confidence=confidence,
            anchored_to=[anchor] if anchor else [],
            drafted_by=DraftedBy.DETERMINISTIC,
            acquisition_tier=AcquisitionTier.MODELING,
        )

    def measure_binding(name: str) -> dict[str, Any] | str:
        placed = index.placed_measure(name)
        if placed is None:
            return f"measure {name!r} was not placed on any relation"
        relation, measure = placed
        if measure["additivity"] == "additive":
            return {"kind": "single", "source": relation, "measure": name}
        column = _distinct_column(measure["expr"])
        if column is None:
            return f"measure {name!r} is neither additive nor a COUNT(DISTINCT column)"
        return {"kind": "distinct_count", "source": relation, "distinct_on": column}

    for source, definition in index.candidates():
        candidate = definition.contract_candidate
        assert candidate is not None  # noqa: S101 — index only returns candidates
        label = f"contract candidate {definition.entity!r} ({definition.native_ref})"
        anchor = definition.source_fingerprint
        if candidate.kind is CandidateKind.DISTINCT_COUNT:
            canonical = measure_binding(candidate.measures[0])
            if isinstance(canonical, str):
                unexpressible.append((source, f"{label}: {canonical}"))
                continue
            aliases = definition.aliases or index.measure_aliases(definition.entity)
            propose(definition.entity, canonical, aliases, anchor)
            continue
        components = [measure_binding(m) for m in candidate.measures]
        problem = next((c for c in components if isinstance(c, str)), None)
        if problem is not None:
            unexpressible.append((source, f"{label}: {problem}"))
            continue
        for name, canonical in zip(candidate.measures, components, strict=True):
            assert isinstance(canonical, dict)  # noqa: S101 — checked above
            propose(name, canonical, index.measure_aliases(name), anchor)
        numerator, denominator = candidate.measures
        propose(
            definition.entity,
            {"kind": "ratio", "numerator": numerator, "denominator": denominator},
            list(definition.aliases),
            anchor,
        )
    return list(proposals.values()), unexpressible


def final_sources(
    accepted: AcceptedStore, entries: list[ReconciliationEntry]
) -> dict[str, dict[str, Any]]:
    """Semantic source name → content after this run: proposed if it lands, else accepted."""
    sources: dict[str, dict[str, Any]] = {}
    for target in accepted.targets():
        fact = accepted.get(target)
        if target.startswith("semantics/") and fact is not None:
            sources[str(fact.content.get("name"))] = fact.content
    for entry in entries:
        if entry.target.startswith("semantics/") and entry.decision in _ADDING:
            sources[str(entry.proposal.content.get("name"))] = entry.proposal.content
    return sources


def _strip(proposal: Proposal) -> Proposal:
    content = {k: v for k, v in proposal.content.items() if k != CANDIDATE_SENTINEL}
    return proposal.model_copy(update={"content": content})


class CandidateResolver:
    """Resolves candidate proposals against the state the rest of the run leaves behind.

    Args:
        accepted: The accepted files.
        entries: The decisions already made for every non-candidate proposal.
        reconcile_new: Applies the normal decision table to a proposal with no accepted
            fact yet (it yields an ``ADD``, possibly marked for auto-apply).
    """

    def __init__(
        self,
        accepted: AcceptedStore,
        entries: list[ReconciliationEntry],
        reconcile_new: Callable[[Proposal], ReconciliationEntry],
    ) -> None:
        self._accepted = accepted
        self._reconcile_new = reconcile_new
        self._sources = final_sources(accepted, entries)
        self._bound: set[str] = {
            str(fact.content.get("metric"))
            for target in accepted.targets()
            if target.startswith("contracts/metrics/")
            and (fact := accepted.get(target)) is not None
        }

    def resolve(self, candidates: list[Proposal]) -> list[ReconciliationEntry]:
        """Decide every candidate, components before the ratios that reference them."""
        entries: list[ReconciliationEntry] = []
        for proposal in candidates:
            stripped = _strip(proposal)
            if self._accepted.get(proposal.target) is not None:
                entries.append(
                    ReconciliationEntry(
                        decision=ReconciliationDecision.NO_OP,
                        target=proposal.target,
                        proposal=stripped,
                        recommended_action="a binding already exists; candidates never change it",
                    )
                )
                continue
            missing = self._missing_reference(stripped.content)
            if missing is not None:
                logger.warning("contract candidate %s not proposed: %s", proposal.target, missing)
                entries.append(
                    ReconciliationEntry(
                        decision=ReconciliationDecision.NO_OP,
                        target=proposal.target,
                        proposal=stripped,
                        recommended_action=f"not proposed: {missing}",
                    )
                )
                continue
            entries.append(self._reconcile_new(stripped))
            self._bound.add(str(stripped.content["metric"]))
        return entries

    def _missing_reference(self, content: dict[str, Any]) -> str | None:
        canonical: dict[str, Any] = content["canonical"]
        kind = canonical["kind"]
        if kind == "ratio":
            for part in ("numerator", "denominator"):
                if canonical[part] not in self._bound:
                    return f"{part} {canonical[part]!r} has no binding"
            return None
        source = self._sources.get(canonical["source"])
        if source is None:
            return f"source {canonical['source']!r} will not exist"
        if kind == "single":
            measures = {m.get("name") for m in source.get("measures", [])}
            if canonical["measure"] not in measures:
                return f"measure {canonical['measure']!r} will not exist on {canonical['source']!r}"
            return None
        columns = {c.get("name") for c in source.get("columns", [])} | {
            d.get("name") for d in source.get("dimensions", [])
        }
        if canonical["distinct_on"] not in columns:
            return f"column {canonical['distinct_on']!r} will not exist on {canonical['source']!r}"
        return None
