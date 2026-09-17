"""Reject oversized request bodies before anything reads them.

Why this exists at all: nothing else in the stack bounds a body. uvicorn sets no
limit, Starlette's multipart parser caps only *non-file* parts (file parts spool
to disk uncapped), and FastAPI reads the body - whole, or spooled - *before* it
solves dependencies, so the auth check and the rate limiters run after the damage
is done. Every `data = await file.read()` in app/core/media_validation.py then
loads the whole thing into RAM before comparing it to a size limit. The result
was that a signed-out `POST /feedback` with a multi-gigabyte attachment, or a
`POST /auth/register` with a multi-gigabyte JSON body, could fill the disk or the
heap of the one backend process for the price of one request.

Two layers, because a `Content-Length` header is a claim and not a measurement:

- **The declared length is checked first**, before the app is entered, so an
  honest oversized request is refused without a byte of its body being received.
- **The bytes are counted as they arrive**, by wrapping `receive`, so a body that
  lies about its length (or is chunked and declares none) is cut off at the same
  cap. The refusal surfaces as the same 413 through FastAPI's body parsing, which
  re-raises `HTTPException` untouched.

Which cap applies is decided by content type rather than by a route list: a
multipart body is the only way anything large legitimately arrives (post media,
feedback attachments, a profile picture), and everything else is a form or a JSON
document that fits in a few KB. A multipart body sent at a JSON route still gets
the larger cap - it is then refused by the route, having cost at most one bounded
spool - which is an accepted simplicity over a list of paths that would silently
go stale the day a route moved. Both numbers are settings (MAX_REQUEST_BODY_BYTES,
MAX_UPLOAD_BODY_BYTES); the upload one must stay above the largest *total* an
upload route accepts plus its framing, or every maximal post fails at the door.

Pure ASGI, like RequestLoggingMiddleware and for the same reason: BaseHTTPMiddleware
cannot wrap `receive`.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.errors import api_error

ERROR_CODE = "request_too_large"


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def body_cap_for(scope: Scope) -> int:
    """The largest body this request may carry, by its declared content type."""
    content_type = (_header(scope, b"content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        return settings.MAX_UPLOAD_BODY_BYTES
    return settings.MAX_REQUEST_BODY_BYTES


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cap = body_cap_for(scope)

        declared = _header(scope, b"content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = -1
            if length < 0 or length > cap:
                await _reject(send, cap)
                return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    # Raised from inside the app's own body read, so it takes the
                    # normal HTTPException path to the structured 413 (FastAPI
                    # re-raises HTTPException from its body parsing untouched).
                    raise api_error(413, ERROR_CODE, max_bytes=cap)
            return message

        await self.app(scope, counting_receive, send)


async def _reject(send: Send, cap: int) -> None:
    """A 413 in the API's own envelope, sent without entering the app."""
    body = json.dumps({"detail": {"error": ERROR_CODE, "max_bytes": cap}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # Tells the client to stop sending the body it was about to send.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
