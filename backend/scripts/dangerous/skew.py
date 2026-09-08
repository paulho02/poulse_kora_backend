"""Fake a lopsided per-channel congestion picture, to see channel pricing move.

Per-channel prices only visibly diverge when two things are true at once: the
channels differ sharply in backlog-per-subscriber (see `pricing.channel_factor`),
and the global base price is high enough that a ±FEED_PRICE_CHANNEL_BAND swing
survives rounding — at a base price of 1-2 the band rounds away entirely and every
channel prices the same, which is correct behaviour but indistinguishable from the
feature being broken. Reproducing that naturally means publishing thousands of posts
and waiting for the refresher to ratchet the price up one token per minute, so this
writes the end state directly instead.

It fabricates: a heavy backlog on channel 2 and none on channel 3, a subscriber
count on channels 1-5, and a price snapshot pinned at 12. Then it prints what
`channel_prices` makes of that — expect channel 2 at ~125% of 12 and channel 3 at
~75%.

**Never run this against production.** It writes junk directly into the live pricing
keys and adds phantom members (`u2-0`, ...) to real channel subscriber sets, which
the fan-out worker will then try to deliver posts to. `scripts/dangerous/cleanup.py`
undoes it. The damage is confined to Redis: `rebuild_redis.py` restores real
membership from Postgres.

Usage (inside the backend container):
    docker compose exec backend python -m scripts.dangerous.skew
"""

import asyncio
import json
import time

from app.core.config import settings
from app.feed import keys, service
from app.redis import redis_client


async def main():
    now = time.time()
    # Channel 2 carries a heavy backlog, channel 3 none - the two ends of the band.
    await redis_client.hset(keys.OPS_OUTSTANDING, mapping={"2": "80", "3": "0"})
    await redis_client.set(keys.SUBS_TOTAL, 40)
    for cid in (1, 2, 3, 4, 5):
        await redis_client.sadd(
            keys.channel(cid), *[f"u{cid}-{i}" for i in range(8)]
        )
        # Drop the cached per-channel price so it is recomputed from the fake state
        # rather than served from the previous window.
        await redis_client.delete(keys.channel_price(cid))
    snap = {
        "price": 12,
        "computed_at": now,
        "expires_at": now + settings.FEED_PRICE_REFRESH_SECONDS,
        "ops_total": await service.outstanding_ops_total(redis_client),
        "subs_total": await service.subscription_total(redis_client),
    }
    await redis_client.set(
        keys.PRICE_SNAPSHOT, json.dumps(snap), ex=settings.FEED_PRICE_TTL_SECONDS
    )
    print("snapshot:", snap)
    print(
        "channel_prices:",
        await service.channel_prices(redis_client, [1, 2, 3, 4, 5]),
    )


asyncio.run(main())
