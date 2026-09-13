"""Rebuild the Redis distribution state that is derivable from Postgres.

Redis is the backbone of the feed algorithm but Postgres remains the source of
truth. Audience sets (the per-channel set and each channel's per-language slices) and
the free-slot set are fully derivable from the `channel_subscriptions` / `users`
tables, token balances are seeded from each user's lifetime `reviewed_count` (a proxy
— actual spends aren't tracked in PG), and the `seen:*` re-delivery guards are seeded
from `post_reviews`.
Per-user queues and the operation queue are NOT derivable and rely on Redis AOF for
durability; this script leaves them untouched.

Also the migration step for enabling FEED_EXCLUDE_SEEN on an existing database: the
`seen:*` sets start empty, so without a run here every user gets one last round of
posts they had already reviewed.

And the required second half of the `0003_content_languages` migration. That migration
adds the columns; the per-language audience sets do not exist until this has run, so
until then a newly created post in a real language finds an empty audience and parks
for retry. Run it as part of that deploy, and again after adding a language to
CONTENT_LANGUAGES.

Idempotent — safe to re-run (e.g. after a Redis flush, or to reconcile drift).

Usage (inside the backend container):
    docker compose exec backend python -m scripts.dangerous.rebuild_redis
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
        f"{stats['seen_seeded']} reviews seeded into seen sets, "
        f"{stats['backfilled']} posts backfilled into queues, "
        f"{stats['outstanding_ops']} outstanding ops recounted for pricing."
    )


if __name__ == "__main__":
    asyncio.run(main())
