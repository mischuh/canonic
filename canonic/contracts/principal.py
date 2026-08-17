"""Principal identity and effective policy — the compiler's authorization inputs (SPEC-E12).

``Principal`` is bound by the adapter (``mcp/auth.py``, the CLI) from a verified token
and passed as a keyword argument to ``compile()`` and every service method — never
accepted from the semantic query itself. This is the load-bearing constraint of the
whole design (AMENDMENT-tenant-scoping-rbac.md, "Design rule: authorization is a
compiler input, never a query field").

Lives under ``contracts/`` rather than ``compiler/`` or ``core/`` because both import
it and neither may import the other's caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canonic.contracts.models import MaskingRule

__all__ = ["SYSTEM_PRINCIPAL", "EffectivePolicy", "Principal"]


@dataclass(frozen=True, slots=True)
class Principal:
    """A caller's verified identity: tenant + role set, bound from a verified token.

    ``tenant`` is ``None`` when no tenancy policy is configured, or when the token
    carries no tenant claim — callers combine this with
    ``ContractResolver.tenancy_enabled`` to decide whether that absence is an error
    (SPEC-E12 §5). ``roles`` is the raw claim value, unresolved against any role
    policy; :meth:`ContractResolver.authz_for` does the resolving.
    """

    tenant: str | None
    roles: tuple[str, ...] = ()
    source: str = "unknown"
    #: Bypasses ``ContractResolver.authz_for``'s role-policy lookup entirely, granting an
    #: unrestricted, ``tenancy_exempt`` :class:`EffectivePolicy` regardless of whatever
    #: ``roles.yaml`` the project declares (or lacks). Never set from a verified token, a
    #: CLI flag, or anything caller-controlled — only :data:`SYSTEM_PRINCIPAL` carries it,
    #: used by the assertion/CI harness and static report validation, both of which check
    #: whether something *compiles* against the full, unfiltered dataset rather than serve
    #: an answer to anyone (SPEC-E12 §7, "assertions run tenant-exempt"). A project-specific
    #: ``tenancy_exempt: true`` role remains the only caller-reachable way to read cross-tenant.
    system_exempt: bool = False


#: The one caller-unreachable :class:`Principal` in the codebase: canonic's own internal
#: checks that a query *compiles*, independent of any tenant — the assertion/CI harness
#: (:mod:`canonic.core.assertions`) and static report validation
#: (:meth:`canonic.core.reports.ReportService.validate_reports`). Never bound from a token,
#: a CLI flag, or anything else caller-controlled.
SYSTEM_PRINCIPAL = Principal(tenant=None, roles=(), source="system", system_exempt=True)


_WILDCARD = "*"


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """A principal's role(s) flattened into one allow/deny surface (SPEC-E12 §1.2, §2).

    Computed once per request by ``ContractResolver.authz_for``, so per-leaf checks
    are a plain set lookup rather than a graph walk. When a principal holds several
    roles, each field is the union of what every assigned role's flattened definition
    grants or denies; ``deny`` always wins over ``allow`` in the final check, so an
    additional role can restrict (via its own denials) as readily as it can expand
    (via its own allowances) — deny-wins is applied uniformly regardless of role count.
    ``"*"`` in an allow set means unrestricted for that dimension.
    """

    allow_metrics: frozenset[str]
    deny_metrics: frozenset[str]
    allow_dimensions: frozenset[str]
    deny_dimensions: frozenset[str]
    allow_tags: frozenset[str]
    run_sql: bool
    tenancy_exempt: bool
    masking: tuple[MaskingRule, ...] = ()
    roles: tuple[str, ...] = ()

    def metric_allowed(self, name: str) -> bool:
        """True if ``name`` is granted by some assigned role and denied by none."""
        if name in self.deny_metrics:
            return False
        return _WILDCARD in self.allow_metrics or name in self.allow_metrics

    def dimension_allowed(self, name: str) -> bool:
        """True if ``name`` is granted by some assigned role and denied by none."""
        if name in self.deny_dimensions:
            return False
        return _WILDCARD in self.allow_dimensions or name in self.allow_dimensions

    def tag_allowed(self, tag: str) -> bool:
        """True if a knowledge page tagged ``tag`` is visible to this policy."""
        return _WILDCARD in self.allow_tags or tag in self.allow_tags
