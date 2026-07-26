"""Lua scripts for the atomic multi-step Redis operations.

Each of these touches more than one key or does a read-modify-write that must not
interleave with another consumer/request:

- ``spend``: check-and-decrement the token balance (no negative balances).
- ``place``: push a post into a recipient's queue (skipping one already there) and
  drop them from ``free_queue`` once full.
- ``claim``: remove a post from a user's queue (the review concurrency guard) and
  re-add them to ``free_queue`` when a slot frees up.

Scripts are registered against a client (cheap, local — just stores the body/SHA)
and memoized per client so tests using the DB-1 client get their own handles.
"""

from dataclasses import dataclass

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

# KEYS[1]=tokens:{user}  ARGV[1]=price
# Returns the new balance, or -1 if the balance is insufficient.
_SPEND = """
local bal = tonumber(redis.call('GET', KEYS[1]) or '0')
local price = tonumber(ARGV[1])
if bal < price then
  return -1
end
return redis.call('DECRBY', KEYS[1], price)
"""

# KEYS[1]=queue:{user}  KEYS[2]=free_queue
# ARGV[1]=post_id  ARGV[2]=user_id  ARGV[3]=max_slots
# Returns the queue length after the push (unchanged if the post was already queued).
#
# Idempotent per (user, post): two independent paths deliver the same post to the same
# user — the worker's fan-out (including an op parked in ops:retry while the channel had
# no free subscriber) and backfill_queue on subscribe. Whichever runs second must not
# push a second copy. LPOS returns false when absent (0 is a valid, truthy index).
_PLACE = """
if redis.call('LPOS', KEYS[1], ARGV[1]) then
  return redis.call('LLEN', KEYS[1])
end
redis.call('LPUSH', KEYS[1], ARGV[1])
local len = redis.call('LLEN', KEYS[1])
if len >= tonumber(ARGV[3]) then
  redis.call('SREM', KEYS[2], ARGV[2])
end
return len
"""

# KEYS[1]=queue:{user}  KEYS[2]=free_queue
# ARGV[1]=post_id  ARGV[2]=user_id  ARGV[3]=max_slots
# Returns the number of items removed (0 if the post was not in the queue).
_CLAIM = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed > 0 then
  local len = redis.call('LLEN', KEYS[1])
  if len < tonumber(ARGV[3]) then
    redis.call('SADD', KEYS[2], ARGV[2])
  end
end
return removed
"""

# KEYS[1]=queue:{user}  KEYS[2]=free_queue  ARGV[1]=user_id  ARGV[2]=max_slots
# Adds the user to free_queue iff their queue currently has room. Returns 1/0.
# Used on subscribe so a new/roomy user becomes reachable by fan-out selection.
_ENSURE_FREE = """
local len = redis.call('LLEN', KEYS[1])
if len < tonumber(ARGV[2]) then
  redis.call('SADD', KEYS[2], ARGV[1])
  return 1
end
return 0
"""


@dataclass(frozen=True)
class FeedScripts:
    spend: AsyncScript
    place: AsyncScript
    claim: AsyncScript
    ensure_free: AsyncScript


_cache: dict[Redis, FeedScripts] = {}


def get_scripts(client: Redis) -> FeedScripts:
    """Return registered Lua script handles for ``client`` (memoized per client)."""
    scripts = _cache.get(client)
    if scripts is None:
        scripts = FeedScripts(
            spend=client.register_script(_SPEND),
            place=client.register_script(_PLACE),
            claim=client.register_script(_CLAIM),
            ensure_free=client.register_script(_ENSURE_FREE),
        )
        _cache[client] = scripts
    return scripts
