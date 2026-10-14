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
from typing import TYPE_CHECKING, Any

from canonic.exc import TenantLimitExceeded, TenantUnresolved

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canonic.contracts.models import MaskingRule

__all__ = [
    "DEFAULT_MAX_TENANTS",
    "SYSTEM_PRINCIPAL",
    "Caller",
    "EffectivePolicy",
    "Principal",
    "TenantScope",
    "TenantValue",
    "normalize_tenants",
    "tenant_sort_key",
]

#: One tenant identifier as carried by a claim. The type is kept as the IdP delivered it, so the
#: compiler can match it to the scope column's type instead of guessing from a string.
TenantValue = int | str

#: ``metadata.scope.tenant`` / ``AnswerEvent.tenant``: a single value for a one-tenant principal
#: (unchanged from before lists were supported), a list for several.
TenantScope = TenantValue | list[TenantValue]

#: Default for ``mcp.auth.max_tenants``, and the limit applied to the ``--tenant`` CLI flag.
DEFAULT_MAX_TENANTS = 100


def tenant_sort_key(value: TenantValue) -> tuple[int, int | str]:
    """Numbers first (numerically), then strings, so mixed lists order deterministically."""
    return (1, value) if isinstance(value, str) else (0, value)


def _tenant_element(raw: Any) -> TenantValue | None:
    """One claim element as a :data:`TenantValue`, or ``None`` when it is null or blank.

    Raises :class:`TenantUnresolved` for a value that cannot be a tenant identifier at all
    (bool, non-integral float, mapping, nested list), rather than guessing a string for it.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise TenantUnresolved("tenant claim contains a boolean, expected a string or integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        return raw if raw.strip() else None
    raise TenantUnresolved(
        f"tenant claim contains a {type(raw).__name__} value, expected a string or integer"
    )


def normalize_tenants(raw: Any, *, limit: int = DEFAULT_MAX_TENANTS) -> tuple[TenantValue, ...]:
    """Turn a raw tenant claim (scalar or list) into a sorted, de-duplicated tuple.

    Null and blank elements are dropped, so ``None``, ``[]`` and ``[None, ""]`` all resolve to
    ``()``, the same as a missing claim. Raises :class:`TenantUnresolved` for an element that
    cannot be a tenant identifier and :class:`TenantLimitExceeded` when more than ``limit``
    distinct values remain. The order is canonical (numbers, then strings), so the same set of
    tenants always compiles to the same SQL regardless of the order the IdP sent it in.
    """
    items: Iterable[Any] = raw if isinstance(raw, list | tuple) else (raw,)
    values = {v for item in items if (v := _tenant_element(item)) is not None}
    if len(values) > limit:
        raise TenantLimitExceeded(
            f"tenant claim carries {len(values)} distinct values, more than the allowed {limit} "
            "(mcp.auth.max_tenants)"
        )
    return tuple(sorted(values, key=tenant_sort_key))


@dataclass(frozen=True, slots=True)
class Principal:
    """A caller's verified identity: tenant(s) + role set, bound from a verified token.

    ``tenants`` is empty when no tenancy policy is configured, or when the token
    carries no tenant claim (or one that resolves to no value) — callers combine this with
    ``ContractResolver.tenancy_enabled`` to decide whether that absence is an error
    (SPEC-E12 §5). ``roles`` is the raw claim value, unresolved against any role
    policy; :meth:`ContractResolver.authz_for` does the resolving.
    """

    tenants: tuple[TenantValue, ...] = ()
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

    @property
    def tenant_scope(self) -> TenantScope | None:
        """The tenants as reported in result metadata and answer events.

        ``None`` for no tenant, the bare value for one, a list for several.
        """
        if not self.tenants:
            return None
        if len(self.tenants) == 1:
            return self.tenants[0]
        return list(self.tenants)


#: The one caller-unreachable :class:`Principal` in the codebase: canonic's own internal
#: checks that a query *compiles*, independent of any tenant — the assertion/CI harness
#: (:mod:`canonic.core.assertions`) and static report validation
#: (:meth:`canonic.core.reports.ReportService.validate_reports`). Never bound from a token,
#: a CLI flag, or anything else caller-controlled.
SYSTEM_PRINCIPAL = Principal(roles=(), source="system", system_exempt=True)


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a verified request is attributed to, for answer events and personal knowledge.

    ``id`` is the identity the answer is *for*: the token's ``client_id`` for a static,
    ``JWTVerifier`` or ``OIDCProxy`` token, or the asserted employee's subject for an
    identity-asserted token (AMENDMENT-e12-identity-assertion-principal). ``acted_via``
    is the calling agent's own ``client_id`` and is set only on the identity-assertion
    path, the one path where "for whom" and "who called" differ.
    """

    id: str
    acted_via: str | None = None


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
        """True if ``name`` is granted by some assigned role and denied by none.

        A join-qualified name (``pickup.city``) is matched on both its full form and its
        bare dimension name (``city``), so a role that denies ``city`` cannot be sidestepped
        by addressing the same dimension through a join alias.
        """
        bare = name.rsplit(".", 1)[-1]
        if name in self.deny_dimensions or bare in self.deny_dimensions:
            return False
        return (
            _WILDCARD in self.allow_dimensions
            or name in self.allow_dimensions
            or bare in self.allow_dimensions
        )

    def tag_allowed(self, tag: str) -> bool:
        """True if a knowledge page tagged ``tag`` is visible to this policy."""
        return _WILDCARD in self.allow_tags or tag in self.allow_tags
