"""Fake a lopsided per-route congestion picture, to see route pricing move.

Route prices only visibly diverge when two things are true at once: the routes differ
sharply in backlog-per-subscriber (see `pricing.route_factor`), and the global base
price is high enough that a ±FEED_PRICE_CHANNEL_BAND swing survives rounding — at a
base price of 1-2 the band rounds away entirely and every route prices the same, which
is correct behaviour but indistinguishable from the feature being broken. Reproducing
that naturally means publishing thousands of posts and waiting for the refresher to
ratchet the price up one token per minute, so this writes the end state directly
instead.

It fabricates: a heavy backlog on channel 2's English route and none on channel 3's,
audience members on channels 1-5, and a price snapshot pinned at 12. Then it prints
what `route_prices` makes of that — expect (2, "en") at ~125% of 12 and (3, "en") at
~75%. It also prints the observed range those computations leave behind, which is what
`GET /posts/economy` reports.

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

from app.core import languages
from app.core.config import settings
from app.feed import keys, service
from app.redis import redis_client


async def main():
    now = time.time()
    channels = (1, 2, 3, 4, 5)
    langs = languages.post_languages()
    # Channel 2's English route carries a heavy backlog, channel 3's none - the two
    # ends of the band.
    await redis_client.hset(
        keys.OPS_OUTSTANDING,
        mapping={keys.route(2, "en"): "80", keys.route(3, "en"): "0"},
    )
    for cid in channels:
        members = [f"u{cid}-{i}" for i in range(8)]
        # Every audience set, not just the plain channel one: a route's price is
        # scaled by the size of the set it actually samples from, so a language route
        # with no members prices at the neutral factor and the skew is invisible.
        await redis_client.sadd(keys.channel(cid), *members)
        for lang in languages.reading_languages():
            await redis_client.sadd(keys.audience(cid, lang), *members)
        for lang in langs:
            # Drop the cached route price so it is recomputed from the fake state
            # rather than served from the previous window.
            await redis_client.delete(keys.route_price(cid, lang))
    # Memberships, not subscriptions - see keys.SUBS_TOTAL.
    await redis_client.set(
        keys.SUBS_TOTAL, len(channels) * 8 * (1 + len(languages.reading_languages()))
    )
    await redis_client.delete(keys.PRICE_RANGE)
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
    routes = [(cid, lang) for cid in channels for lang in langs]
    prices = await service.route_prices(redis_client, routes)
    for route in routes:
        print(f"  {route[0]}/{route[1]}: {prices[route]}")
    print("observed range:", await service.read_price_range(redis_client))


asyncio.run(main())
