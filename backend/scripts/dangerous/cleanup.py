"""Undo `scripts/dangerous/skew.py` — drop the fabricated pricing and channel state.

Deletes the two pricing counters, the global price snapshot, and (for channel ids
1-19) both the subscriber set and the cached per-channel price. The next refresher
tick republishes a snapshot from real congestion, and `rebuild_redis.py` restores
real channel membership from Postgres.

**Never run this against production.** It does not distinguish the phantom
subscribers `skew.py` injected from real ones — it deletes the whole
`channel:{id}` set either way, so on a live deployment it drops real subscriptions
from Redis until a rebuild puts them back. Nothing here touches Postgres, so a
rebuild is always sufficient to recover; that is the only reason this is a cleanup
script rather than an outage.

Usage (inside the backend container):
    docker compose exec backend python -m scripts.dangerous.cleanup
    docker compose exec backend python -m scripts.dangerous.rebuild_redis
"""

import asyncio

from app.feed import keys
from app.redis import redis_client


async def main():
    await redis_client.delete(
        keys.OPS_OUTSTANDING, keys.SUBS_TOTAL, keys.PRICE_SNAPSHOT
    )
    for cid in range(1, 20):
        await redis_client.delete(keys.channel(cid), keys.channel_price(cid))
    print("cleared skewed keys - run rebuild_redis to restore real membership")


asyncio.run(main())
