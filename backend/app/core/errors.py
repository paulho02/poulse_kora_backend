"""Structured API errors.

Every error response the API emits carries the same body shape::

    {"detail": {"error": "<machine_readable_code>", ...extra}}

The `error` code is the contract the Flutter client keys its user-facing copy off
(see `lib/src/core/errors/error_messages.dart`) — it must stay stable, whereas any
prose in `message` is only a developer aid. `factory.setup_exception_handlers`
normalizes the responses FastAPI and fastapi-users raise on their own into this
shape too, so a client never has to branch on where an error came from.
"""

import re

from fastapi import HTTPException

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def api_error(status_code: int, error: str, **extra) -> HTTPException:
    """Build an `HTTPException` with the structured detail body.

    Usage: `raise api_error(404, "post_not_found")`, or with context the client can
    render: `raise api_error(402, "insufficient_tokens", balance=3, price=5)`.
    """
    return HTTPException(status_code, {"error": error, **extra})


def slugify_detail(detail: str) -> str:
    """Turn a stock HTTP reason phrase into a code, e.g. `Not Found` -> `not_found`.

    Only used as the fallback for exceptions raised outside our own routes (Starlette
    internals, fastapi-users). Our routes should always pass an explicit code.
    """
    return _NON_SLUG.sub("_", detail.strip().lower()).strip("_") or "error"
