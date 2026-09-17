"""One log line per HTTP request, and the request id everything else hangs off.

Pure ASGI middleware rather than `@app.middleware("http")`/`BaseHTTPMiddleware`,
for two reasons that both matter here: `BaseHTTPMiddleware` runs the rest of the
app in a separate task (so a context bound by a *dependency* would not be visible
to the line written after the response), and it cannot see the response status
without buffering. Wrapping `send` gives the status, the headers and the duration
for free.

**What it produces.** Exactly one record per request, `http.request`, carrying
method, route, status and duration — plus, via the context it opens, the request
id and user id that every other line emitted while serving that request also
carries. That is the join: one filter on `@request_id` reconstructs a request end
to end, including the traceback from four modules down.

**Level is chosen by outcome, not by taste**, so a deployed log at INFO is
readable without filters and a spike is visible without a dashboard:

- 5xx → ERROR. 429 → WARNING (someone is being throttled). Other 4xx → INFO.
- slower than `LOG_SLOW_REQUEST_MS` → WARNING, whatever the status. A route that
  starts taking two seconds is the thing you want to have noticed before users
  say so.
- `LOG_QUIET_PATHS` → DEBUG. `/health` (Railway's own probe, plus the client's
  reconnect loop) and `/posts/feed/status` (polled while the feed is on screen)
  are the two endpoints whose call rate is set by timers rather than by people;
  at a few hundred users they would drown everything that means anything.

**The request id.** Taken from an inbound `X-Request-ID` if a client or proxy set
one (so a mobile-app breadcrumb and the server line share it), otherwise minted
here. It goes back on the response header, into the body of a 500, and onto
`scope` — the last one because Starlette's 500 handler runs *outside* this
middleware, after the context has been reset, and reading it off the scope dict
is the one place it is still reachable there.

**Errors are logged here, once.** An unhandled exception passes through this
frame before Starlette's `ServerErrorMiddleware` turns it into a response, and
this frame is the only one with the request context bound — so the traceback is
written here (`http.request_failed`) and the exception handler in app/factory.py
deliberately does not write a second copy of it.
"""

import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.logger import (
    get_logger,
    new_request_context,
    reset_request_context,
    running_on_railway,
)

log = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"

#: Long enough that a collision is not a thing, short enough to be read down a
#: phone to support.
_REQUEST_ID_LENGTH = 16


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def caller_address(scope: Scope, behind_proxy: bool) -> str | None:
    """The caller's address, as far as we are willing to believe it.

    Two deployments, two answers, and *not* a setting. Behind Railway's edge the
    socket peer is always the proxy, so reading it there yields one constant
    internal address on every line; in front of no proxy, `X-Forwarded-For` is
    just text the caller sent. Which of those holds is a fact about where the
    process is running rather than a preference, so it is derived from the same
    Railway detection that picks the log format — a flag here would be one whose
    wrong value degrades silently, exactly the kind this codebase declines to
    make expressible (compare `FEED_SEEN_TTL_SECONDS`).

    **The rightmost entry, not the leftmost.** A proxy *appends* to
    `X-Forwarded-For`, so the header reads `<whatever the caller claimed>, <the
    address the proxy actually saw>`. The last entry is the one our own trusted
    hop wrote; the first is the caller's to invent. Taking the last is also
    correct for a proxy that overwrites the header instead of appending, so it
    holds either way.

    Shared with the signed-out rate limiters (see app/deps/rate_limit.py), which
    need the same answer for the opposite reason: keyed on the socket peer behind
    the proxy, every anonymous caller on the deployment would share one budget,
    and keyed on the *leftmost* forwarded entry the budget would be the caller's
    to reset. One derivation, so the two cannot disagree.
    """
    if behind_proxy:
        forwarded = _header(scope, b"x-forwarded-for")
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip()[:45]
    client = scope.get("client")
    return client[0] if client else None


def _client_ip(scope: Scope, behind_proxy: bool) -> str | None:
    """`caller_address`, gated by `LOG_CLIENT_IP` - an address is personal data,
    so the log line carries it only where the deployment has opted in."""
    if not settings.LOG_CLIENT_IP:
        return None
    return caller_address(scope, behind_proxy)


def _route_path(scope: Scope) -> str | None:
    """The route *template* (`/api/v1/posts/{post_id}`) when routing has resolved
    one — the low-cardinality field to group by. The raw path is kept alongside it
    for the cases it hasn't (a 404, a 401 rejected before the handler)."""
    route = scope.get("route")
    return getattr(route, "path", None)


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._quiet = set(settings.LOG_QUIET_PATHS)
        # Resolved once: where this process runs cannot change while it runs.
        self._behind_proxy = running_on_railway()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = (
            _header(scope, REQUEST_ID_HEADER.encode()) or uuid.uuid4().hex
        )[:_REQUEST_ID_LENGTH]
        scope["request_id"] = request_id

        token = new_request_context(
            request_id=request_id,
            method=scope["method"],
            path=scope["path"],
            client_ip=_client_ip(scope, self._behind_proxy),
        )
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The only frame that has both the exception and the request context.
            log.exception(
                "http.request_failed",
                status=500,
                duration_ms=_elapsed_ms(started),
                route=_route_path(scope),
            )
            raise
        else:
            if settings.LOG_ACCESS:
                self._log_request(scope, status, _elapsed_ms(started))
        finally:
            reset_request_context(token)

    def _log_request(self, scope: Scope, status: int, duration_ms: float) -> None:
        slow = duration_ms >= settings.LOG_SLOW_REQUEST_MS
        if status >= 500:
            level = "error"
        elif status == 429 or slow:
            level = "warning"
        elif status >= 400:
            level = "info"
        elif scope["path"] in self._quiet:
            level = "debug"
        else:
            level = "info"
        getattr(log, level)(
            "http.request",
            status=status,
            duration_ms=duration_ms,
            route=_route_path(scope),
            slow=slow or None,
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
