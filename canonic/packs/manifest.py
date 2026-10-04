"""Pydantic models for ``pack.yaml`` (AMENDMENT-context-packs §2.2, §5.4)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_RELEASE = re.compile(r"\d+(?:\.\d+)*")


def _release_tuple(version: str) -> tuple[int, ...] | None:
    """The leading numeric release of ``version`` (``0.32.0.dev1`` gives ``(0, 32, 0)``), or None."""
    match = _RELEASE.match(version)
    return tuple(int(part) for part in match.group().split(".")) if match else None


__all__ = [
    "ChooseFrom",
    "DeriveSpec",
    "FirstAnswer",
    "Param",
    "ParamValidate",
    "PackManifest",
    "Provides",
    "ProvidesContracts",
    "Variant",
]


class ChooseFrom(BaseModel):
    """A pick-list param source: a live read-only query, or another param's list (§5.4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str | None = None
    same_as: str | None = None
    allow_custom: bool = True

    @model_validator(mode="after")
    def _validate_shape(self) -> ChooseFrom:
        if (self.query is None) == (self.same_as is None):
            raise ValueError("choose_from must set exactly one of 'query' or 'same_as'")
        return self


class ParamValidate(BaseModel):
    """A regex a param's resolved value must match before use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: str


class DeriveSpec(BaseModel):
    """Build a param's value from another param's plain answer (§5.4.2)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    from_: str = Field(alias="from")
    per_value: str
    join: str = ""
    when_empty: str = ""


class Param(BaseModel):
    """One declared install-time parameter."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    name: str
    description: str = ""
    required: bool = False
    default: str | None = None
    choose_from: ChooseFrom | None = None
    derive: DeriveSpec | None = None
    validate_: ParamValidate | None = Field(default=None, alias="validate")


class ProvidesContracts(BaseModel):
    """The ``contracts`` sub-list of ``provides``: metric and guardrail file paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: list[str] = []
    guardrails: list[str] = []


class Provides(BaseModel):
    """Pack-relative paths of every file this pack emits, grouped by target surface."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    semantics: list[str] = []
    contracts: ProvidesContracts = ProvidesContracts()
    knowledge: list[str] = []

    def merged_with(self, other: Provides) -> Provides:
        """These paths followed by ``other``'s, per surface."""
        return Provides(
            semantics=[*self.semantics, *other.semantics],
            contracts=ProvidesContracts(
                metrics=[*self.contracts.metrics, *other.contracts.metrics],
                guardrails=[*self.contracts.guardrails, *other.contracts.guardrails],
            ),
            knowledge=[*self.knowledge, *other.knowledge],
        )

    def all_paths(self) -> list[str]:
        """Every listed path, surface by surface, in install order."""
        return [
            *self.semantics,
            *self.contracts.metrics,
            *self.contracts.guardrails,
            *self.knowledge,
        ]


class Variant(BaseModel):
    """One installable variant of a pack (a physical mapping onto a connector type).

    ``provides``, ``required_tables`` and ``params`` are the variant's own content
    (AMENDMENT-pack-variant-content §2.1). Read them through
    :meth:`PackManifest.for_variant`, which merges them into the pack-wide values.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str
    mapping: str
    connector: str | None = None
    provides: Provides | None = None
    required_tables: list[str] | None = None
    params: list[Param] = []


class FirstAnswer(BaseModel):
    """§5.6: the demo query the setup flow runs right after install."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    window: str | None = None


def _check_param_names(params: list[Param], *, where: str) -> None:
    names = [p.name for p in params]
    if len(names) != len(set(names)):
        raise ValueError(f"{where}duplicate param name in 'params'")


def _check_params(params: list[Param], *, where: str) -> None:
    """Unique names, and every ``same_as``/``derive.from`` names a param in the same list."""
    _check_param_names(params, where=where)
    names = {p.name for p in params}
    for p in params:
        if p.choose_from is not None and p.choose_from.same_as is not None:
            target = p.choose_from.same_as
            if target not in names:
                raise ValueError(
                    f"{where}param {p.name!r} choose_from.same_as references unknown param "
                    f"{target!r}"
                )
        if p.derive is not None and p.derive.from_ not in names:
            raise ValueError(
                f"{where}param {p.name!r} derive.from references unknown param {p.derive.from_!r}"
            )


def _check_provides(provides: Provides, *, where: str) -> None:
    """No path listed twice, and no two knowledge files sharing a name.

    Knowledge pages are written flat into ``knowledge/global/``, so the file name decides
    the target. The target of a semantic source or contract comes from the name inside the
    file, which only the installer can see.
    """
    for path, count in Counter(provides.all_paths()).items():
        if count > 1:
            raise ValueError(f"{where}path {path!r} is listed more than once in 'provides'")
    knowledge_names = Counter(PurePosixPath(path).name for path in provides.knowledge)
    for name, count in knowledge_names.items():
        if count > 1:
            raise ValueError(f"{where}knowledge files share the file name {name!r}")


class PackManifest(BaseModel):
    """The parsed, validated ``pack.yaml`` (§2.2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack: str
    version: str
    description: str = ""
    homepage: str | None = None
    min_canonic_version: str | None = None
    variants: list[Variant]
    params: list[Param] = []
    required_tables: list[str] = []
    provides: Provides
    first_answer: FirstAnswer | None = None

    @field_validator("min_canonic_version")
    @classmethod
    def _validate_min_canonic_version(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(_RELEASE, value):
            raise ValueError(
                f"min_canonic_version must be a plain release such as '0.32.0', got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _validate_refs(self) -> PackManifest:
        if not self.variants:
            raise ValueError("pack.yaml must declare at least one variant")
        variant_ids = [v.id for v in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("duplicate variant id in 'variants'")

        _check_params(self.params, where="")
        _check_provides(self.provides, where="")
        for v in self.variants:
            if v.provides is None and v.required_tables is None and not v.params:
                continue
            where = f"variant {v.id!r}: "
            _check_param_names(v.params, where=where)
            resolved = self.for_variant(v.id)
            _check_params(resolved.params, where=where)
            _check_provides(resolved.provides, where=where)
        return self

    def check_compatible(self, canonic_version: str | None = None) -> None:
        """Raise PackError when the running canonic is older than ``min_canonic_version``.

        ``canonic_version`` defaults to the installed release. An unknown version (a source
        checkout without package metadata) is not blocked.
        """
        from canonic import __version__
        from canonic.exc import PackError

        if self.min_canonic_version is None:
            return
        running = canonic_version if canonic_version is not None else __version__
        current = _release_tuple(running)
        required = _release_tuple(self.min_canonic_version)
        if current is None or required is None or current >= required:
            return
        raise PackError(
            f"pack {self.pack!r} {self.version} needs canonic {self.min_canonic_version} or newer, "
            f"but this is canonic {running}. Upgrade canonic and try again."
        )

    def variant(self, variant_id: str) -> Variant:
        """The declared variant with this id, or raise PackError listing the known ones."""
        from canonic.exc import PackError

        for v in self.variants:
            if v.id == variant_id:
                return v
        known = ", ".join(v.id for v in self.variants)
        raise PackError(f"unknown variant {variant_id!r} for pack {self.pack!r}; known: {known}")

    def for_variant(self, variant_id: str) -> PackManifest:
        """The manifest that applies when ``variant_id`` is installed.

        ``provides`` is the pack-wide paths followed by the variant's. ``required_tables``
        is the variant's list when it sets one, else the pack-wide list. ``params`` are the
        pack-wide params, each replaced by the variant's param of the same name, followed by
        the variant's other params (AMENDMENT-pack-variant-content §2.4).

        The result declares only the chosen variant, with its own content cleared, so
        resolving it again returns it unchanged.
        """
        variant = self.variant(variant_id)
        overrides = {p.name: p for p in variant.params}
        params = [overrides.pop(p.name, p) for p in self.params]
        params.extend(overrides.values())
        return self.model_copy(
            update={
                "variants": [
                    variant.model_copy(
                        update={"provides": None, "required_tables": None, "params": []}
                    )
                ],
                "provides": (
                    self.provides
                    if variant.provides is None
                    else self.provides.merged_with(variant.provides)
                ),
                "required_tables": (
                    self.required_tables
                    if variant.required_tables is None
                    else variant.required_tables
                ),
                "params": params,
            }
        )

    def param(self, name: str) -> Param | None:
        """The declared param with this name, or None."""
        return next((p for p in self.params if p.name == name), None)
