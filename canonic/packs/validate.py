"""Validate a pack with no project and no live connection (``canonic pack validate``).

Reuses :func:`canonic.packs.install.install_pack` almost unchanged — it already does
template substitution, ``provenance``/``pack_source`` stamping, writing, and full
E5/E15/E6 validation with zero DB dependency (only the separately-called
:func:`canonic.packs.install.check_required_tables` touches a connection, and this module
never calls it). What's missing for a connection-free, project-free validation run is
synthetic param values and a scratch directory — both supplied here.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from canonic.exc import PackError
from canonic.packs.install import InstallResult, install_pack, synthesize_params
from canonic.packs.templating import substitute

if TYPE_CHECKING:
    from canonic.packs.manifest import PackManifest, Variant

__all__ = ["validate_pack"]


def _check_templates(manifest: PackManifest, params: dict[str, str]) -> list[str]:
    """Template-only checks with zero execution — catches a typo'd ``{{param}}`` in a
    ``required_tables`` entry or a ``choose_from.query``, neither of which
    :func:`~canonic.packs.install.install_pack` itself ever renders (the former is
    connector-check-only, the latter is interactive-prompt-only)."""
    errors: list[str] = []
    for t in manifest.required_tables:
        try:
            substitute(t, params, source="pack.yaml#required_tables")
        except PackError as exc:
            errors.append(str(exc))
    for p in manifest.params:
        if p.choose_from is None or p.choose_from.query is None:
            continue
        try:
            substitute(p.choose_from.query, params, source=f"pack.yaml#params.{p.name}.choose_from")
        except PackError as exc:
            errors.append(str(exc))
    return errors


def validate_pack(pack_dir: Path, manifest: PackManifest, variant: Variant) -> InstallResult:
    """Install ``manifest``/``variant`` into a discarded scratch directory with
    synthesized params, and return the resulting :class:`~canonic.packs.install.InstallResult`
    (``validation_errors`` empty means the pack validated cleanly)."""
    params = synthesize_params(manifest)
    template_errors = _check_templates(manifest, params)

    with tempfile.TemporaryDirectory(prefix=f"canonic-pack-validate-{manifest.pack}-") as tmp:
        result = install_pack(Path(tmp), pack_dir, manifest, variant, params)

    result.validation_errors = template_errors + result.validation_errors
    return result
