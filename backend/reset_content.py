"""Reset all feed content while leaving user accounts untouched.

Deletes every post, post review, and channel subscription, then clears the
corresponding Redis distribution state (per-user queues, channel subscriber
sets, seen sets, token balances, the operation stream and its retry queue) and
rebuilds it from the now-empty Postgres state — same reconciliation
`rebuild_from_pg` does after a Redis loss, just starting from a clean slate.

Kept as-is:
- Channels (reference data, not content — users would otherwise have nothing
  left to subscribe to).
- Users: credentials, profile, verification state, Google linking, and any
  `UserSubscription`/`SupporterSubscription` (payment) rows. The one thing
  touched on the user row is `reviewed_count`/`forwarded_count`/
  `dropped_count`, zeroed because they tally the review history this script
  deletes — leaving them non-zero would desync the token balance `rebuild_from_pg`
  seeds from `reviewed_count`.

Usage (inside the backend container):
    docker compose exec backend python reset_content.py [--yes]
"""

import argparse
import asyncio

from sqlalchemy import delete, update

from app.db import async_session_maker
from app.feed import keys, service
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.post_media import PostMedia
from app.models.post_review import PostReview
from app.models.user import User
from app.redis import redis_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    return parser.parse_args()


async def _delete_pattern(redis, pattern: str) -> int:
    count = 0
    async for key in redis.scan_iter(match=pattern):
        await redis.delete(key)
        count += 1
    return count


async def main():
    args = parse_args()
    if not args.yes:
        confirm = input(
            "This permanently deletes ALL posts, reviews, and channel "
            "subscriptions (channels and user accounts are kept). "
            "Type RESET to continue: "
        )
        if confirm != "RESET":
            print("Aborted.")
            return

    async with async_session_maker() as session:
        reviews_deleted = (await session.execute(delete(PostReview))).rowcount
        # No DB-level ON DELETE CASCADE from post_blocks/post_media to posts (same
        # as post_reviews) - must delete child rows before the FK-referenced
        # parent. post_blocks first: it FKs to both posts and post_media.
        await session.execute(delete(PostBlock))
        await session.execute(delete(PostMedia))
        posts_deleted = (await session.execute(delete(Post))).rowcount
        subs_deleted = (await session.execute(delete(ChannelSubscription))).rowcount
        await session.execute(
            update(User).values(reviewed_count=0, forwarded_count=0, dropped_count=0)
        )
        await session.commit()

        queues_cleared = await _delete_pattern(redis_client, "queue:*")
        channels_cleared = await _delete_pattern(redis_client, "channel:*")
        seen_cleared = await _delete_pattern(redis_client, "seen:*")
        tokens_cleared = await _delete_pattern(redis_client, "tokens:*")
        await redis_client.delete(
            keys.FREE_QUEUE, keys.STREAM, keys.OPS_RETRY, keys.PRICE_SNAPSHOT
        )

        await service.ensure_group(redis_client)
        redis_stats = await service.rebuild_from_pg(redis_client, session)

    print(
        f"Deleted {reviews_deleted} reviews, {posts_deleted} posts, "
        f"{subs_deleted} channel subscriptions. Reset activity counters for all users.\n"
        f"Redis cleared: {queues_cleared} queues, {channels_cleared} channel sets, "
        f"{seen_cleared} seen sets, {tokens_cleared} token balances, plus "
        f"free_queue/operation stream/retry queue/price snapshot.\n"
        f"Redis rebuilt: {redis_stats['users']} users re-seeded with starting tokens."
    )


if __name__ == "__main__":
    asyncio.run(main())
