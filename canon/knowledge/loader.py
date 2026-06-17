"""Load knowledge/**/*.md files into typed KnowledgePage models (SPEC-E6 §2).

A page is Markdown with YAML frontmatter delimited by ``---`` fences. ``scope`` and
``id`` are derived from the file path, never read from the frontmatter — a frontmatter
that hand-sets either is rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from canon.exc import KnowledgePageError
from canon.knowledge.models import KnowledgePage, KnowledgeScope, KnowledgeValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = [
    "load_knowledge_page",
    "scope_from_path",
    "slug_from_path",
    "user_from_path",
]

_KNOWLEDGE_DIR = "knowledge"
_FENCE = "---"
# Frontmatter keys the loader derives from the path; rejected if hand-set (SPEC-E6 §2).
_DERIVED_KEYS = ("scope", "id", "path")


def scope_from_path(path: Path) -> KnowledgeScope:
    """Derive a page's scope from its path (SPEC-E6 §4).

    ``knowledge/global/…`` → GLOBAL, ``knowledge/user/<id>/…`` → USER. Raises
    KnowledgePageError if the path does not sit under a recognized scope.
    """
    parts = path.parts
    try:
        i = parts.index(_KNOWLEDGE_DIR)
    except ValueError:
        raise KnowledgePageError(
            f"{path}: not under a '{_KNOWLEDGE_DIR}/' directory; cannot derive scope"
        ) from None
    segment = parts[i + 1] if i + 1 < len(parts) else ""
    try:
        return KnowledgeScope(segment)
    except ValueError:
        raise KnowledgePageError(
            f"{path}: unknown scope segment {segment!r}; "
            f"expected '{_KNOWLEDGE_DIR}/global/…' or '{_KNOWLEDGE_DIR}/user/<id>/…'"
        ) from None


def slug_from_path(path: Path) -> str:
    """Derive a page's id/slug from its filename (SPEC-E6 §2: filename stem)."""
    return path.stem


def user_from_path(path: Path) -> str | None:
    """Owner id of a USER-scoped page (``knowledge/user/<id>/…``), or ``None`` for GLOBAL.

    The ``<id>`` segment carries the page owner that scope visibility filters on (SPEC-E6 §4);
    GLOBAL pages have no owner. Raises KnowledgePageError if a ``user/`` path omits the owner
    directory (e.g. ``knowledge/user/note.md``), since such a page belongs to no one.
    """
    if scope_from_path(path) is KnowledgeScope.GLOBAL:
        return None
    parts = path.parts
    i = parts.index(_KNOWLEDGE_DIR)
    # parts[i+1] is the scope segment ("user"); parts[i+2] is the owner id, and it must be a
    # directory — the filename itself (the last part) cannot stand in for the owner.
    owner_idx = i + 2
    if owner_idx >= len(parts) - 1:
        raise KnowledgePageError(
            f"{path}: user page has no '<id>' owner segment; "
            f"expected '{_KNOWLEDGE_DIR}/user/<id>/…'"
        )
    return parts[owner_idx]


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split page text into (yaml_frontmatter, body).

    Recognizes a leading ``---`` fence, the YAML block, and a closing ``---`` fence;
    everything after is the body (verbatim). A file without a leading fence is treated
    as all body with empty frontmatter.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            yaml_block = "".join(lines[1:i])
            body = "".join(lines[i + 1 :])
            return yaml_block, body
    # Opening fence with no closing fence: treat the whole thing as body.
    return "", text


def _line_for_path(raw: Any, path: Iterable[str | int]) -> int | None:
    """Best-effort 1-based line for a YAML path, walking ruamel's `.lc` data."""
    node = raw
    line: int | None = None
    for key in path:
        lc = getattr(node, "lc", None)
        data = getattr(lc, "data", None)
        if not isinstance(data, dict) or key not in data:
            break
        line = data[key][0]  # ruamel rows are 0-based
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            break
    return None if line is None else line + 1


def _raise_located(path: Path, raw: Any, loc: Iterable[str | int], message: str) -> None:
    line = _line_for_path(raw, loc)
    where = f"{path}:{line}" if line is not None else str(path)
    raise KnowledgePageError(f"{where}: {message}")


def load_knowledge_page(path: Path) -> KnowledgePage:
    """Load and validate one knowledge page, raising KnowledgePageError.

    ``scope`` and ``id`` are derived from ``path``; a frontmatter that hand-sets a
    derived key (``scope``, ``id``, ``path``) is rejected. The error message carries
    ``file:line`` for the offending node where it can be located.
    """
    if not path.exists():
        raise KnowledgePageError(f"knowledge page not found: {path}")

    try:
        text = path.read_text()
    except OSError as exc:
        raise KnowledgePageError(f"{path}: cannot read file: {exc}") from exc

    yaml_block, body = _split_frontmatter(text)

    yaml = YAML()  # round-trip mode: loaded nodes carry `.lc` line/col data
    try:
        raw: Any = yaml.load(yaml_block) or {}
    except Exception as exc:  # noqa: BLE001 — any parse failure is a page error
        raise KnowledgePageError(f"{path}: cannot parse frontmatter YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise KnowledgePageError(f"{path}: frontmatter must be a mapping")

    for key in _DERIVED_KEYS:
        if key in raw:
            raise KnowledgePageError(
                f"{path}: {key!r} is derived from the path and must not be set in frontmatter"
            )

    data = {
        **raw,
        "id": slug_from_path(path),
        "path": path,
        "scope": scope_from_path(path),
        "body": body,
    }

    try:
        return KnowledgePage.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        located = err.get("ctx", {}).get("error")
        if isinstance(located, KnowledgeValidationError):
            _raise_located(path, raw, located.path, str(located))
        loc = err["loc"]
        msg = err["msg"]
        suffix = " → ".join(str(p) for p in loc)
        message = f"{suffix}: {msg}" if suffix else msg
        _raise_located(path, raw, loc, message)
        raise AssertionError("unreachable") from exc  # _raise_located always raises
