"""Rebuild the Redis distribution state that is derivable from Postgres.

Redis is the backbone of the feed algorithm but Postgres remains the source of
truth. Channel subscriber sets and the free-slot set are fully derivable from the
`channel_subscriptions` / `users` tables, and token balances are seeded from each
user's lifetime `reviewed_count` (a proxy — actual spends aren't tracked in PG).
Per-user queues and the operation queue are NOT derivable and rely on Redis AOF for
durability; this script leaves them untouched.

Idempotent — safe to re-run (e.g. after a Redis flush, or to reconcile drift).

Usage (inside the backend container):
    docker compose exec backend python rebuild_redis.py
"""

import asyncio

from app.db import async_session_maker
from app.feed.service import rebuild_from_pg
from app.redis import redis_client


async def main():
    async with async_session_maker() as session:
        stats = await rebuild_from_pg(redis_client, session)
    print(
        f"Rebuilt Redis state: {stats['subscriptions']} subscriptions across "
        f"channel sets, {stats['users']} users seeded (free_queue + tokens), "
        f"{stats['backfilled']} posts backfilled into queues."
    )


if __name__ == "__main__":
    asyncio.run(main())
