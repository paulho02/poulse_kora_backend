"""Per-user rate limiting for feed write interactions (create / forward / drop).

Two things it protects: the operation queue, which a scripted client could flood
faster than the worker fans out, and the *meaning* of a review — five forwards in a
second is not five people reading five posts, and that signal is what the whole
distribution algorithm rests on.

Implemented as a **sliding window log**: one Redis sorted set per user holding the
timestamp of each interaction currently inside the window. A single Lua script does
the expire-check-record sequence atomically, so it costs one round trip per request
and two concurrent requests cannot both slip through on a stale count. The set holds
at most `limit` members and carries a `PEXPIRE` of the window length, so an idle
user costs nothing — no sweeper, no background job.

Why not a plain `INCR` fixed window (the cheaper, more common choice): at a window
boundary it admits twice the limit back to back (5 hits at t=9.9s, 5 more at
t=10.1s), which is precisely the burst this exists to stop. The log is the same
single round trip, gives exact semantics, and yields a truthful `retry_after`.

No library: `fastapi-limiter` and `slowapi` both key on IP + route path, whereas we
need one budget per *user* shared across three different routes, raising the
project's structured error envelope (`app/core/errors.py`). Bending either to that
is more code than the fifteen lines of Lua below.
"""

import hashlib
import math
from uuid import uuid4

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from app.core.config import settings
from app.core.errors import api_error
from app.core.logger import get_logger

log = get_logger(__name__)

# KEYS[1]=rate:{scope}:{user}
# ARGV[1]=window_ms  ARGV[2]=limit  ARGV[3]=unique member id
#
# Returns 0 when the interaction is allowed (and has been recorded), otherwise the
# milliseconds until the oldest recorded hit falls out of the window.
#
# The clock is Redis's own (`TIME`), not the caller's: with several app processes
# writing to the same key, client clock skew would otherwise widen or narrow the
# window unpredictably. Allowed inside scripts under effects replication (Redis 5+).
_CONSUME = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - window)
if redis.call('ZCARD', KEYS[1]) >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return math.ceil(tonumber(oldest[2]) + window - now_ms)
end
redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return 0
"""

_cache: dict[Redis, AsyncScript] = {}


def _script(client: Redis) -> AsyncScript:
    """Registered handle for the script (memoized per client, like FeedScripts)."""
    script = _cache.get(client)
    if script is None:
        script = client.register_script(_CONSUME)
        _cache[client] = script
    return script


def key(scope: str, user_id: str) -> str:
    """Window log for one user within one budget, e.g. `rate:interact:<uuid>`."""
    return f"rate:{scope}:{user_id}"


async def consume(
    redis: Redis, scope: str, user_id: str, limit: int, window_seconds: float
) -> int:
    """Try to spend one slot from the user's budget.

    Returns 0 when the interaction is allowed — the slot is recorded as part of the
    same atomic call — or the milliseconds the caller must wait before retrying.
    """
    window_ms = int(window_seconds * 1000)
    result = await _script(redis)(
        keys=[key(scope, user_id)], args=[window_ms, limit, uuid4().hex]
    )
    return int(result)


def opaque_identity(prefix: str, value: str) -> str:
    """A budget identity for something that is not an id - an email address being
    tried at login, say - without putting the value itself into a Redis key or a
    log line. Normalized (trimmed, lowercased) before hashing, so `Alice@` and
    `alice@` draw on the same budget."""
    digest = hashlib.sha256(value.strip().lower().encode()).hexdigest()[:32]
    return f"{prefix}:{digest}"


def rejection(
    scope: str, identity: str, retry_ms: int, limit: int, window_seconds: float
) -> HTTPException:
    """The 429 every limiter raises, logged once at WARNING.

    WARNING rather than INFO because a limit being hit is by definition unusual:
    the budgets are set well above what the UI can produce by hand, so a burst
    means either a client bug (a retry loop) or someone driving the API directly.
    The identity is logged because it is the only thing that tells those apart -
    except that an `ip:` identity is personal data, so it is written only where
    `LOG_CLIENT_IP` has opted the deployment in, the same rule the request line
    follows.

    `retry_after` is rounded up and never advertised as 0 seconds - a client
    obeying it would retry immediately and be rejected again.
    """
    retry_after = max(1, math.ceil(retry_ms / 1000))
    if identity.startswith("ip:") and not settings.LOG_CLIENT_IP:
        identity = "ip:<redacted>"
    log.warning(
        "rate_limit.exceeded",
        scope=scope,
        identity=identity,
        retry_after=retry_after,
        limit=limit,
    )
    exc = api_error(
        429,
        "rate_limited",
        retry_after=retry_after,
        limit=limit,
        window_seconds=window_seconds,
    )
    # Standard header alongside the structured body, so non-app clients (and any
    # proxy in front of us) see the backoff too. `setup_exception_handlers` passes
    # `exc.headers` through to the response.
    exc.headers = {"Retry-After": str(retry_after)}
    return exc


async def enforce(
    redis: Redis, scope: str, identity: str, limit: int, window_seconds: float
) -> None:
    """`consume`, raising the 429 when the budget is spent. A limit of 0 disables
    the budget, which every setting that feeds this documents as its off switch."""
    if limit <= 0:
        return
    retry_ms = await consume(redis, scope, identity, limit, window_seconds)
    if retry_ms > 0:
        raise rejection(scope, identity, retry_ms, limit, window_seconds)
