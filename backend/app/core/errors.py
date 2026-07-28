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


def detail_text(value) -> str:
    """Stringify an exception-detail value for `slugify_detail`/display.

    fastapi-users raises its own errors with `ErrorCode(str, Enum)` members as the
    detail (e.g. `ErrorCode.LOGIN_BAD_CREDENTIALS`) - calling the builtin `str()` on
    one does NOT return the string value ("LOGIN_BAD_CREDENTIALS"), it returns
    Enum's own `__str__` ("ErrorCode.LOGIN_BAD_CREDENTIALS"), because a `(str, Enum)`
    mixin doesn't inherit `str`'s `__str__`. That garbled text was slipping into the
    API's `error`/`message` fields (e.g. `errorcode_login_bad_credentials`) for
    every bare-string fastapi-users error. Since such values already *are* `str`
    instances with the right character data, returning them as-is sidesteps the
    `__str__` override entirely; only genuinely non-str values fall back to `str()`.
    """
    return value if isinstance(value, str) else str(value)
