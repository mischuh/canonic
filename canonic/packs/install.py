"""Install a context pack into a project (AMENDMENT-context-packs §2, §5).

The core routine :func:`install_pack` is called from both ``canonic pack add`` and the
``canonic setup`` wizard's pack branch — two entry points into the same install routine
(§5). It writes ordinary, already-schema-valid E5/E15/E6 files; nothing here is a new
file format or a new query/validation path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ruamel.yaml import YAML

from canonic.connectors.base import Capability
from canonic.connectors.factory import default_factory
from canonic.contracts.validate import validate_contracts
from canonic.exc import CanonicError, PackError
from canonic.knowledge.loader import load_knowledge_page
from canonic.knowledge.validation import EntityIndex, PageIndex, ReferenceValidator
from canonic.packs.derive import apply_derive, validate_param_value
from canonic.packs.meta import stamp_contract_yaml, stamp_knowledge_markdown, stamp_semantic_yaml
from canonic.packs.templating import substitute
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from canonic.config import Connection
    from canonic.connectors.base import SchemaIntrospectable
    from canonic.packs.manifest import PackManifest, Param, Variant

logger = logging.getLogger(__name__)

__all__ = [
    "InstallResult",
    "check_required_tables",
    "install_pack",
    "resolve_params",
    "synthesize_params",
]

_SEMANTICS_DIR = "semantics"
_METRICS_DIR = "contracts/metrics"
_GUARDRAILS_DIR = "contracts/guardrails"
_KNOWLEDGE_GLOBAL_DIR = "knowledge/global"


@dataclass
class InstallResult:
    """What :func:`install_pack` did — the CLI renders a table from this, the wizard
    proceeds to the first-answer step."""

    pack: str
    version: str
    variant: str
    params: dict[str, str]
    written: list[Path] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


def resolve_params(manifest: PackManifest, explicit: dict[str, str]) -> dict[str, str]:
    """Resolve every declared param to a final string value (non-interactive path, §5.8).

    Precedence: an explicit value (flag/params-file/wizard answer) always wins over a
    derived value (§5.4.2) or a default. Missing required params are reported together
    in one PackError, not one at a time.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for p in manifest.params:
        if p.name in explicit:
            resolved[p.name] = explicit[p.name]
        elif p.derive is not None:
            continue  # resolved below, once every plain param is known
        elif p.default is not None:
            resolved[p.name] = p.default
        elif p.required:
            missing.append(p.name)
    if missing:
        raise PackError(
            f"missing required param(s) for pack {manifest.pack!r}: {', '.join(sorted(missing))}"
        )

    for p in manifest.params:
        if p.derive is None or p.name in resolved:
            continue
        source_value = resolved.get(p.derive.from_, "")
        resolved[p.name] = apply_derive(p.derive, source_value)

    for p in manifest.params:
        if p.name in resolved:
            validate_param_value(p.name, resolved[p.name], p.validate_)

    return resolved


#: Non-empty placeholder for a param feeding a ``derive.from`` (see :func:`synthesize_params`).
#: A single lowercase word satisfies every ``validate.pattern`` shipped so far (comma-list
#: patterns over letters, or letters/digits/./-); :func:`synthesize_params` still falls back
#: to the param's own default rather than risk violating a stricter pattern it can't predict.
_DERIVE_SOURCE_PLACEHOLDER = "example"


def synthesize_params(manifest: PackManifest) -> dict[str, str]:
    """Placeholder values for every declared param — the "no real answers" case used by
    ``canonic pack validate`` (no connection, no interactive prompts, no ``--params-file``).

    A param that feeds a ``derive.from`` gets a non-empty synthetic value so the derive's
    ``per_value`` template is actually exercised — the ``when_empty`` branch is a literal
    string with no substitution, so there is nothing in it worth validating — unless doing
    so would violate the param's own declared ``validate.pattern``, in which case its
    default (or "") is used instead: never fabricate a value that breaks the pack's own
    declared constraint. Every other param gets its declared default, or a generic
    placeholder (covers ``choose_from`` params, whose query is never executed here, and any
    plain required param with no default). Routed through :func:`resolve_params` so pattern
    validation still runs on the synthesized values.
    """
    derive_sources = {p.derive.from_ for p in manifest.params if p.derive is not None}
    explicit: dict[str, str] = {}
    for p in manifest.params:
        if p.derive is not None:
            continue  # computed below, once every plain param is known
        if p.name in derive_sources:
            explicit[p.name] = _safe_derive_source_value(p)
        elif p.default is not None:
            explicit[p.name] = p.default
        else:
            explicit[p.name] = f"example_{p.name}"

    for p in manifest.params:
        if p.derive is None:
            continue
        explicit[p.name] = apply_derive(p.derive, explicit.get(p.derive.from_, ""))

    return resolve_params(manifest, explicit)


def _safe_derive_source_value(p: Param) -> str:
    """``_DERIVE_SOURCE_PLACEHOLDER`` if it satisfies ``p``'s own ``validate.pattern``,
    else ``p.default`` (or "") — see :func:`synthesize_params`."""
    try:
        validate_param_value(p.name, _DERIVE_SOURCE_PLACEHOLDER, p.validate_)
    except PackError:
        return p.default if p.default is not None else ""
    return _DERIVE_SOURCE_PLACEHOLDER


def check_required_tables(
    manifest: PackManifest, params: dict[str, str], connection: Connection
) -> None:
    """§2.4: every required table must exist on ``connection`` before anything is written."""
    if not manifest.required_tables:
        return
    required = [
        substitute(t, params, source="pack.yaml#required_tables") for t in manifest.required_tables
    ]
    live = set(_introspect_relations(connection))
    missing = [t for t in required if t not in live]
    if missing:
        raise PackError(
            f"required table(s) not found on connection {connection.id!r}: {', '.join(missing)}"
        )


def _introspect_relations(connection: Connection) -> list[str]:
    async def _run() -> list[str]:
        connector = default_factory.create(connection)
        try:
            if Capability.INTROSPECT_SCHEMA not in connector.capabilities():
                raise PackError(
                    f"connection {connection.id!r} ({connection.type}) cannot introspect its "
                    "schema; a context pack's required_tables check needs it"
                )
            relations = await cast("SchemaIntrospectable", connector).introspect_schema()
            return [r.relation for r in relations]
        finally:
            await connector.aclose()

    return asyncio.run(_run())


def install_pack(
    project_root: Path,
    pack_dir: Path,
    manifest: PackManifest,
    variant: Variant,
    params: dict[str, str],
) -> InstallResult:
    """Template-substitute, stamp, and write every file in ``manifest.provides``.

    Caller must have already run :func:`check_required_tables`. Writes every target file
    unconditionally — a version-bump reinstall overwrites the same way ``canonic ingest``
    overwrites prior inferred content (§2.3) — then runs the project's ordinary E5/E15/E6
    loaders/validators over the whole project. A validation failure is recorded in
    ``InstallResult.validation_errors`` and reported, but nothing already written is
    deleted: an installed file is indistinguishable from a hand-written one the moment it
    lands, so a broken one is fixed the same way any other broken committed file is.
    """
    result = InstallResult(
        pack=manifest.pack, version=manifest.version, variant=variant.id, params=dict(params)
    )

    for rel in manifest.provides.semantics:
        result.written.append(
            _write_semantic_file(project_root, pack_dir, rel, params, manifest, variant)
        )
    for rel in manifest.provides.contracts.metrics:
        result.written.append(
            _write_contract_file(
                project_root, pack_dir, rel, params, manifest, variant, kind="metric"
            )
        )
    for rel in manifest.provides.contracts.guardrails:
        result.written.append(
            _write_contract_file(
                project_root, pack_dir, rel, params, manifest, variant, kind="guardrail"
            )
        )
    for rel in manifest.provides.knowledge:
        result.written.append(
            _write_knowledge_file(project_root, pack_dir, rel, params, manifest, variant)
        )

    result.validation_errors = _validate_project(project_root)
    return result


def _read_yaml_field(text: str, field_name: str, *, source: str) -> str:
    data = YAML().load(text) or {}
    value = data.get(field_name)
    if not value:
        raise PackError(f"{source}: missing required {field_name!r} field")
    return str(value)


def _write_semantic_file(
    project_root: Path,
    pack_dir: Path,
    rel: str,
    params: dict[str, str],
    manifest: PackManifest,
    variant: Variant,
) -> Path:
    text = substitute((pack_dir / rel).read_text(), params, source=rel)
    text = stamp_semantic_yaml(
        text, pack=manifest.pack, version=manifest.version, variant=variant.id
    )
    name = _read_yaml_field(text, "name", source=rel)
    target = project_root / _SEMANTICS_DIR / params["connection_id"] / f"{name}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def _write_contract_file(
    project_root: Path,
    pack_dir: Path,
    rel: str,
    params: dict[str, str],
    manifest: PackManifest,
    variant: Variant,
    *,
    kind: str,
) -> Path:
    text = substitute((pack_dir / rel).read_text(), params, source=rel)
    text = stamp_contract_yaml(
        text, pack=manifest.pack, version=manifest.version, variant=variant.id
    )
    field_name = "metric" if kind == "metric" else "id"
    subdir = _METRICS_DIR if kind == "metric" else _GUARDRAILS_DIR
    name = _read_yaml_field(text, field_name, source=rel)
    target = project_root / subdir / f"{name}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def _write_knowledge_file(
    project_root: Path,
    pack_dir: Path,
    rel: str,
    params: dict[str, str],
    manifest: PackManifest,
    variant: Variant,
) -> Path:
    text = substitute((pack_dir / rel).read_text(), params, source=rel)
    text = stamp_knowledge_markdown(
        text, pack=manifest.pack, version=manifest.version, variant=variant.id
    )
    target = project_root / _KNOWLEDGE_GLOBAL_DIR / Path(rel).name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def _validate_project(project_root: Path) -> list[str]:
    """Run the existing E5/E15/E6 loaders/validators; collect failures instead of raising."""
    errors: list[str] = []
    try:
        sources = list_semantic_sources(project_root)
    except CanonicError as exc:
        return [str(exc)]  # contracts/knowledge validation both need sources; stop here

    try:
        validate_contracts(project_root)
    except CanonicError as exc:
        errors.append(str(exc))

    knowledge_dir = project_root / "knowledge"
    if knowledge_dir.is_dir():
        try:
            pages = [load_knowledge_page(p) for p in sorted(knowledge_dir.rglob("*.md"))]
        except CanonicError as exc:
            errors.append(str(exc))
        else:
            entity_index = EntityIndex.from_sources(sources)
            page_index = PageIndex.from_pages(pages)
            validator = ReferenceValidator(entity_index, page_index)
            for page in pages:
                try:
                    validator.validate_page(page)
                except CanonicError as exc:
                    errors.append(str(exc))

    return errors
