"""Structured error serialisation for MCP tool responses (SPEC §6.1).

Every P0 tool wraps its call with ``canonic_error_response`` so that any
:class:`canonic.exc.CanonicError` becomes a typed ``{code, message, candidates?}``
dict rather than an unhandled exception, enabling agent clients to refuse-and-ask
instead of fabricating an answer.
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any

from canonic.exc import CanonicError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["canonic_error_response", "error_payload"]


def error_payload(exc: CanonicError) -> dict[str, Any]:
    """Serialise a :class:`CanonicError` to the canonical §6.1 wire shape."""
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


def canonic_error_response(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: catch :class:`CanonicError` and return a structured error dict.

    Applied to every MCP tool so the tool never propagates a raw exception to the
    agent client — it always returns either a successful result or an error payload.
    """

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except CanonicError as exc:
            return error_payload(exc)

    return wrapper
