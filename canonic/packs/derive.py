"""§5.4.2 ``derive``: build a predicate param from a plain comma-separated answer."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from canonic.exc import PackError

if TYPE_CHECKING:
    from canonic.packs.manifest import DeriveSpec, ParamValidate

__all__ = ["apply_derive", "validate_param_value"]


def validate_param_value(name: str, value: str, validate: ParamValidate | None) -> None:
    """Check ``value`` against ``validate.pattern`` (full match), raising PackError on failure.

    An empty value is never checked against the pattern: a ``validate``d param is always
    paired with "leave empty to skip" semantics (e.g. ``internal_email_domains``), and a
    pattern written for the non-empty case has no obligation to also match "".
    """
    if validate is None or value == "":
        return
    if re.fullmatch(validate.pattern, value) is None:
        raise PackError(
            f"param {name!r} value {value!r} does not match pattern {validate.pattern!r}"
        )


def apply_derive(spec: DeriveSpec, source_value: str) -> str:
    """Render ``spec.per_value`` for each comma-separated item in ``source_value``, joined.

    An empty (post-strip) ``source_value`` yields ``spec.when_empty`` — the "leave it
    empty to skip" behavior for e.g. ``internal_email_domains``.
    """
    values = [v.strip() for v in source_value.split(",") if v.strip()]
    if not values:
        return spec.when_empty
    rendered = [spec.per_value.replace("{{value}}", v) for v in values]
    return spec.join.join(rendered)
