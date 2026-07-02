"""Air-gapped egress guard (SPEC-E10 §4).

The enforced privacy differentiator: under ``runtime.air_gapped: true`` no warehouse
content or context may leave the machine/network. :class:`EgressPolicy` is the single
source of truth for *what counts as local*, shared by load-time config validation
(``canonic/config.py``) and call-time runtime enforcement (``canonic/runtime/generation.py``)
so the allowlist logic is never duplicated.

Default policy is **localhost-only** (loopback + the name ``localhost``): the tightest
guarantee. A separate on-prem inference host must be opted in explicitly via
``runtime.allow_cidrs``. Dependency-light (stdlib only) so the config layer can import it
without pulling in litellm.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from canonic.exc import AirGappedViolation

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["LOCAL_REF_SCHEMES", "EgressPolicy", "guard_telemetry"]

#: Secret-reference schemes that resolve on-machine. A ``*_ref`` using any other scheme
#: (e.g. a future ``vault:``/``https:`` remote secret service) is rejected under air-gapped.
LOCAL_REF_SCHEMES: frozenset[str] = frozenset({"env", "keyring", "file"})

#: Always-allowed loopback ranges, independent of any configured allowlist.
_LOOPBACK_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def guard_telemetry(*, air_gapped: bool, telemetry_enabled: bool) -> None:
    """Raise AirGappedViolation if telemetry would be on while air-gapped (SPEC-E16 §9, S5).

    The single chokepoint for the air-gapped telemetry guarantee. Load-time config validation
    and any future opt-in telemetry-enable path both call this so the guard cannot be bypassed.
    """
    if air_gapped and telemetry_enabled:
        raise AirGappedViolation(
            "air-gapped: telemetry.enabled must be false when runtime.air_gapped is true"
        )


class EgressPolicy:
    """Allowlist guard for air-gapped egress (SPEC-E10 §4).

    Loopback addresses and the literal hostname ``localhost`` are always allowed.
    ``allow_cidrs`` adds explicit private/LAN ranges for an on-prem inference host. An
    instance is only created when ``runtime.air_gapped`` is set — its mere existence
    means enforcement is active.
    """

    def __init__(self, *, allow_cidrs: Sequence[str] = ()) -> None:
        extra: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for cidr in allow_cidrs:
            try:
                extra.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError as exc:
                raise AirGappedViolation(
                    f"runtime.allow_cidrs entry {cidr!r} is not a valid CIDR: {exc}"
                ) from exc
        self._networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
            *_LOOPBACK_NETWORKS,
            *extra,
        )

    def check_url(self, url: str, *, what: str) -> None:
        """Raise :class:`AirGappedViolation` if ``url``'s host is not allowlisted.

        Args:
            url: The endpoint URL whose host is checked (e.g. ``llm.base_url``).
            what: Names the configuration source for a clear message.
        """
        host = urlsplit(url).hostname
        if host is None:
            raise AirGappedViolation(f"air-gapped: {what} {url!r} has no resolvable host to check")
        if not self.is_allowed_host(host):
            raise AirGappedViolation(
                f"air-gapped: {what} host {host!r} is not local or allowlisted — "
                f"add it to runtime.allow_cidrs or use a local endpoint"
            )

    def check_ref_local(self, ref: str, *, what: str) -> None:
        """Raise :class:`AirGappedViolation` if ``ref`` uses a non-local secret scheme.

        Forward-proofs against a future remote secret service: only ``env:``/``keyring:``/
        ``file:`` resolve on-machine (:data:`LOCAL_REF_SCHEMES`).
        """
        scheme = ref.partition(":")[0]
        if scheme not in LOCAL_REF_SCHEMES:
            raise AirGappedViolation(
                f"air-gapped: {what} {ref!r} uses non-local secret scheme {scheme!r}; "
                f"only {', '.join(sorted(LOCAL_REF_SCHEMES))} are allowed"
            )

    def is_allowed_host(self, host: str) -> bool:
        """Whether ``host`` resolves only to allowlisted addresses.

        An IP literal is checked directly. ``localhost`` is special-cased to avoid relying
        on resolver configuration. Any other hostname is resolved via ``getaddrinfo`` and
        **every** resolved address must be allowlisted — fail-closed when resolution yields
        nothing or any address falls outside the allowlist.
        """
        if host == "localhost":
            return True

        literal = self._as_ip(host)
        if literal is not None:
            return self._is_allowed_address(literal)

        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        addresses = {str(info[4][0]) for info in infos}
        if not addresses:
            return False
        return all(
            (addr := self._as_ip(raw)) is not None and self._is_allowed_address(addr)
            for raw in addresses
        )

    def _is_allowed_address(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(address in network for network in self._networks)

    @staticmethod
    def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(value)
        except ValueError:
            return None
