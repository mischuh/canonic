"""One-time, offline ``{{param}}`` substitution for pack content (AMENDMENT-context-packs §2.3).

Plain string replacement, deliberately not a templating engine: the amendment is explicit
that this runs once at install time, and every emitted file is byte-for-byte a normal,
static project file afterward — no templating engine exists at compile or serve time.
"""

from __future__ import annotations

import re

from canonic.exc import PackError

__all__ = ["substitute"]

_TOKEN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def substitute(text: str, params: dict[str, str], *, source: str) -> str:
    """Replace every ``{{name}}`` token in ``text`` with ``params[name]``.

    Raises PackError naming ``source`` (the pack-relative file the token came from) and
    the unresolved token name if a template references a param that was never resolved —
    a pack-authoring bug, caught before the result ever touches the project.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise PackError(f"{source}: unresolved template token {{{{{name}}}}}")
        return params[name]

    return _TOKEN.sub(_replace, text)
