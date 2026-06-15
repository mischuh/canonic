"""Structured error serialisation for MCP tool responses (SPEC §6.1).

Every P0 tool wraps its call with ``canon_error_response`` so that any
:class:`canon.exc.CanonError` becomes a typed ``{code, message, candidates?}``
dict rather than an unhandled exception, enabling agent clients to refuse-and-ask
instead of fabricating an answer.
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any

from canon.exc import CanonError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["canon_error_response", "error_payload"]


def error_payload(exc: CanonError) -> dict[str, Any]:
    """Serialise a :class:`CanonError` to the canonical §6.1 wire shape."""
    payload: dict[str, Any] = {
        "code": exc.code.value if exc.code is not None else "internal_error",
        "message": str(exc),
    }
    if exc.candidates:
        # Candidates may be Pydantic models or plain objects; serialise defensively.
        serialised = []
        for c in exc.candidates:
            if hasattr(c, "model_dump"):
                serialised.append(c.model_dump())
            else:
                serialised.append(str(c))
        payload["candidates"] = serialised
    return payload


def canon_error_response(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: catch :class:`CanonError` and return a structured error dict.

    Applied to every MCP tool so the tool never propagates a raw exception to the
    agent client — it always returns either a successful result or an error payload.
    """

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except CanonError as exc:
            return error_payload(exc)

    return wrapper
