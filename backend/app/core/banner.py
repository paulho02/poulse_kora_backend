"""The single app-wide info banner (superuser-set, publicly readable).

One JSON key holding the current message, if any. No TTL: unlike the feed's
price snapshot (app/feed/service.py), this must persist indefinitely until an
admin explicitly replaces or clears it — there is no "stale" state to expire
out of.
"""

import json
import time
import uuid
from typing import TypedDict

from redis.asyncio import Redis

KEY = "banner:current"


class Banner(TypedDict):
    id: str
    message: str
    set_at: float


async def get_banner(redis: Redis) -> Banner | None:
    """The current banner, or None if nothing has been set (or it was cleared)."""
    raw = await redis.get(KEY)
    if raw is None:
        return None
    return json.loads(raw)


async def set_banner(redis: Redis, message: str) -> Banner:
    """Upsert the banner with a fresh id.

    The id is random rather than derived from the message text, so re-pushing
    identical text is still a new event: a client that dismissed an earlier
    message "forever" is unaffected by this one.
    """
    banner: Banner = {"id": uuid.uuid4().hex, "message": message, "set_at": time.time()}
    await redis.set(KEY, json.dumps(banner))
    return banner


async def clear_banner(redis: Redis) -> None:
    await redis.delete(KEY)
