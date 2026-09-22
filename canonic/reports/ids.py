"""Id generation for saved queries and composed reports.

Same slugify shape as ``canonic/cli/commands/knowledge.py:_slugify`` (a filesystem-safe, readable
prefix), plus a short content hash so two saves of the same/blank title never collide on disk.
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["generate_id", "slugify"]


def slugify(text: str) -> str:
    """Filesystem-safe slug derived from *text*; ``"item"`` if that strips to nothing."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def generate_id(title: str, *, seed: str) -> str:
    """A short, readable, collision-resistant id: ``<slug>-<hash6>``.

    ``seed`` (e.g. the query's compiled JSON, or a timestamp) feeds the hash so repeated saves of
    the same title still produce distinct ids.
    """
    digest = hashlib.sha256(seed.encode()).hexdigest()[:6]
    return f"{slugify(title)}-{digest}"
