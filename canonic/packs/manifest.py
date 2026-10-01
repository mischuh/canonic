"""Pydantic models for ``pack.yaml`` (AMENDMENT-context-packs §2.2, §5.4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

    model_config = ConfigDict(frozen=True)

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

    model_config = ConfigDict(frozen=True)

    pattern: str


class DeriveSpec(BaseModel):
    """Build a param's value from another param's plain answer (§5.4.2)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    from_: str = Field(alias="from")
    per_value: str
    join: str = ""
    when_empty: str = ""


class Param(BaseModel):
    """One declared install-time parameter."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str
    description: str = ""
    required: bool = False
    default: str | None = None
    choose_from: ChooseFrom | None = None
    derive: DeriveSpec | None = None
    validate_: ParamValidate | None = Field(default=None, alias="validate")


class Variant(BaseModel):
    """One installable variant of a pack (a physical mapping onto a connector type)."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    mapping: str
    connector: str | None = None


class ProvidesContracts(BaseModel):
    """The ``contracts`` sub-list of ``provides``: metric and guardrail file paths."""

    model_config = ConfigDict(frozen=True)

    metrics: list[str] = []
    guardrails: list[str] = []


class Provides(BaseModel):
    """Pack-relative paths of every file this pack emits, grouped by target surface."""

    model_config = ConfigDict(frozen=True)

    semantics: list[str] = []
    contracts: ProvidesContracts = ProvidesContracts()
    knowledge: list[str] = []


class FirstAnswer(BaseModel):
    """§5.6: the demo query the setup flow runs right after install."""

    model_config = ConfigDict(frozen=True)

    metric: str
    window: str | None = None


class PackManifest(BaseModel):
    """The parsed, validated ``pack.yaml`` (§2.2)."""

    model_config = ConfigDict(frozen=True)

    pack: str
    version: str
    description: str = ""
    homepage: str | None = None
    variants: list[Variant]
    params: list[Param] = []
    required_tables: list[str] = []
    provides: Provides
    first_answer: FirstAnswer | None = None

    @model_validator(mode="after")
    def _validate_refs(self) -> PackManifest:
        if not self.variants:
            raise ValueError("pack.yaml must declare at least one variant")
        variant_ids = [v.id for v in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("duplicate variant id in 'variants'")

        param_names = {p.name for p in self.params}
        if len(param_names) != len(self.params):
            raise ValueError("duplicate param name in 'params'")

        for p in self.params:
            if p.choose_from is not None and p.choose_from.same_as is not None:
                target = p.choose_from.same_as
                if target not in param_names:
                    raise ValueError(
                        f"param {p.name!r} choose_from.same_as references unknown param {target!r}"
                    )
            if p.derive is not None and p.derive.from_ not in param_names:
                raise ValueError(
                    f"param {p.name!r} derive.from references unknown param {p.derive.from_!r}"
                )
        return self

    def variant(self, variant_id: str) -> Variant:
        """The declared variant with this id, or raise PackError listing the known ones."""
        from canonic.exc import PackError

        for v in self.variants:
            if v.id == variant_id:
                return v
        known = ", ".join(v.id for v in self.variants)
        raise PackError(f"unknown variant {variant_id!r} for pack {self.pack!r}; known: {known}")

    def param(self, name: str) -> Param | None:
        """The declared param with this name, or None."""
        return next((p for p in self.params if p.name == name), None)
