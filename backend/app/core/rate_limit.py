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

from uuid import uuid4

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

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
