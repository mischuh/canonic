"""Stamp ``provenance``/``pack_source`` onto templated pack content before it is written (§2.3).

Every emitted semantic source, metric binding, guardrail, and knowledge page gets
``provenance: human_curated`` and a ``pack_source: {pack, version, variant}`` tag — an
additive field (``SourceMeta.pack_source`` / ``MetricBinding.pack_source`` /
``Guardrail.pack_source`` / ``KnowledgePageMeta.pack_source``), same status as ``frozen``:
system-written, human-editable afterward, not itself validated by the compiler.
"""

from __future__ import annotations

import io
from typing import Any

from ruamel.yaml import YAML

__all__ = ["stamp_contract_yaml", "stamp_knowledge_markdown", "stamp_semantic_yaml"]

_FENCE = "---"


def _pack_source_dict(pack: str, version: str, variant: str) -> dict[str, str]:
    return {"pack": pack, "version": version, "variant": variant}


def _load_yaml(text: str) -> Any:
    return YAML().load(text) or {}


def _dump_yaml(data: Any) -> str:
    yaml = YAML()
    yaml.default_flow_style = False
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    return buffer.getvalue()


def stamp_semantic_yaml(text: str, *, pack: str, version: str, variant: str) -> str:
    """Set ``meta.provenance``/``meta.pack_source`` on a templated semantic source."""
    data = _load_yaml(text)
    meta = data.setdefault("meta", {})
    meta["provenance"] = "human_curated"
    meta["pack_source"] = _pack_source_dict(pack, version, variant)
    return _dump_yaml(data)


def stamp_contract_yaml(text: str, *, pack: str, version: str, variant: str) -> str:
    """Set top-level ``provenance``/``pack_source`` on a templated metric or guardrail file."""
    data = _load_yaml(text)
    data["provenance"] = "human_curated"
    data["pack_source"] = _pack_source_dict(pack, version, variant)
    return _dump_yaml(data)


def stamp_knowledge_markdown(text: str, *, pack: str, version: str, variant: str) -> str:
    """Set ``meta.provenance``/``meta.pack_source`` in a knowledge page's frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        return text  # no frontmatter fence — nothing to stamp into
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == _FENCE), None)
    if close is None:
        return text

    frontmatter_text = "".join(lines[1:close])
    body = "".join(lines[close + 1 :])
    data = _load_yaml(frontmatter_text)
    meta = data.setdefault("meta", {})
    meta["provenance"] = "human_curated"
    meta["pack_source"] = _pack_source_dict(pack, version, variant)
    return f"{_FENCE}\n{_dump_yaml(data)}{_FENCE}\n{body}"
