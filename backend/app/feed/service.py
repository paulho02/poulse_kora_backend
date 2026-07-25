"""High-level async helpers over the Redis client for the feed algorithm.

Postgres stays the source of truth for posts/reviews/subscriptions; everything here
manipulates only distribution *state* (queues, sets, counters, the operation queue),
storing post_ids rather than content.
"""

import json
import time
import uuid
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.feed import keys
from app.feed.scripts import get_scripts
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.user import User


# --- operation queue -------------------------------------------------------

async def enqueue_operation(redis: Redis, post_id: int, channel_id: int) -> None:
    """Push a fan-out operation for `post_id` onto the tail of the `ops` queue."""
    await redis.rpush(
        keys.OPS, json.dumps({"post_id": post_id, "channel_id": channel_id})
    )


async def operation_queue_len(redis: Redis) -> int:
    """Current length of the operation queue (drives admission pricing)."""
    return await redis.llen(keys.OPS)


async def schedule_retry(
    redis: Redis, post_id: int, channel_id: int, delay: float | None = None
) -> None:
    """Park an undeliverable operation in `ops:retry`, due after `delay` seconds
    (defaults to `FEED_RETRY_INTERVAL_SECONDS`).

    Held in a sorted set rather than requeued immediately, so a channel with no
    free recipients doesn't spin the consumer in a tight retry loop. The member
    is prefixed with a random id so two retries for the same post/channel pair
    (e.g. a create and a later forward, both stalled) never collide as a single
    sorted-set entry.
    """
    if delay is None:
        delay = settings.FEED_RETRY_INTERVAL_SECONDS
    payload = json.dumps({"post_id": post_id, "channel_id": channel_id})
    member = f"{uuid4().hex}:{payload}"
    await redis.zadd(keys.OPS_RETRY, {member: time.time() + delay})


async def reschedule_due_retries(redis: Redis, now: float | None = None) -> int:
    """Move due operations from `ops:retry` back onto the front of `ops`.

    Uses LPUSH (not RPUSH): a retried operation is necessarily older than
    anything created since it stalled, so re-inserting at the head lets it cut
    back in front of newer operations instead of losing its place in line on
    every retry cycle. Returns the number of operations rescheduled.
    """
    if now is None:
        now = time.time()
    due = await redis.zrangebyscore(keys.OPS_RETRY, "-inf", now)
    for member in due:
        _, _, payload = member.partition(":")
        # LPUSH before ZREM: if the process dies in between, the op is merely
        # duplicated (already-tolerated, see claim_from_queue/review_post's
        # IntegrityError handling) rather than silently lost.
        await redis.lpush(keys.OPS, payload)
        await redis.zrem(keys.OPS_RETRY, member)
    return len(due)


# --- tokens ----------------------------------------------------------------

async def token_balance(redis: Redis, user_id: str) -> int:
    """Current spendable token balance (0 if the counter does not exist yet)."""
    raw = await redis.get(keys.tokens(user_id))
    return int(raw) if raw is not None else 0


async def earn_token(redis: Redis, user_id: str, amount: int = 1) -> int:
    """Increment a user's balance (called on every review). Returns the new balance."""
    return await redis.incrby(keys.tokens(user_id), amount)


async def spend_tokens(redis: Redis, user_id: str, price: int) -> int | None:
    """Atomically spend `price` tokens if the balance covers it.

    Returns the new balance, or None when the balance is insufficient (no change).
    """
    result = await get_scripts(redis).spend(
        keys=[keys.tokens(user_id)], args=[price]
    )
    return None if int(result) < 0 else int(result)


# --- queues / free_queue ---------------------------------------------------

async def place_post(redis: Redis, user_id: str, post_id: int) -> int:
    """Push a post into a recipient's queue, dropping them from free_queue if full.

    Returns the queue length after the push.
    """
    return await get_scripts(redis).place(
        keys=[keys.queue(user_id), keys.FREE_QUEUE],
        args=[post_id, user_id, settings.FEED_QUEUE_MAX_SLOTS],
    )


async def claim_from_queue(redis: Redis, user_id: str, post_id: int) -> int:
    """Remove one occurrence of `post_id` from the user's queue (review guard).

    Re-adds the user to free_queue if a slot freed up. Returns the number removed
    (0 means the post was not in the queue).
    """
    return await get_scripts(redis).claim(
        keys=[keys.queue(user_id), keys.FREE_QUEUE],
        args=[post_id, user_id, settings.FEED_QUEUE_MAX_SLOTS],
    )


async def render_queue_ids(
    redis: Redis, user_id: str, limit: int, skip: int = 0
) -> list[int]:
    """Return up to `limit` post_ids from the user's queue (from `skip`), in order."""
    raw = await redis.lrange(keys.queue(user_id), skip, skip + limit - 1)
    return [int(pid) for pid in raw]


# --- recipient selection ---------------------------------------------------

async def select_recipients(redis: Redis, channel_id: int, k: int) -> list[str]:
    """Pick up to `k` distinct random user_ids subscribed to the channel *and* free.

    Server-side: SINTERSTORE the channel's subscribers with free_queue into a temp
    key, then SRANDMEMBER `k` distinct members.
    """
    tmp = f"tmp:sinter:{uuid4().hex}"
    try:
        await redis.sinterstore(tmp, [keys.channel(channel_id), keys.FREE_QUEUE])
        return await redis.srandmember(tmp, k)
    finally:
        await redis.delete(tmp)


# --- subscription sync -----------------------------------------------------

async def sync_subscribe(redis: Redis, user_id: str, channel_id: int) -> None:
    """Reflect a subscription: add to the channel set, ensure reachable via free_queue."""
    await redis.sadd(keys.channel(channel_id), user_id)
    await get_scripts(redis).ensure_free(
        keys=[keys.queue(user_id), keys.FREE_QUEUE],
        args=[user_id, settings.FEED_QUEUE_MAX_SLOTS],
    )


async def sync_unsubscribe(redis: Redis, user_id: str, channel_id: int) -> None:
    """Reflect an unsubscription: remove the user from the channel set."""
    await redis.srem(keys.channel(channel_id), user_id)


async def backfill_queue(
    redis: Redis, session: AsyncSession, user_id: uuid.UUID, channel_id: int
) -> int:
    """Seed a fresh subscriber's queue with recent un-reviewed posts from the channel.

    Live distribution is push-based (operations → worker fan-out), but a user who
    subscribes after posts already fanned out would otherwise see nothing. This
    fills the remaining free slots with the newest channel posts they haven't yet
    reviewed. Returns the number of posts backfilled.
    """
    current_ids = await render_queue_ids(
        redis, str(user_id), settings.FEED_QUEUE_MAX_SLOTS
    )
    slots = settings.FEED_QUEUE_MAX_SLOTS - len(current_ids)
    if slots <= 0:
        return 0

    already_reviewed = select(PostReview.post_id).filter(
        PostReview.user_id == user_id
    )
    post_ids = (
        (
            await session.execute(
                select(Post.id)
                .filter(
                    Post.channel_id == channel_id,
                    Post.id.notin_(already_reviewed),
                    # Skip posts already queued so re-runs don't duplicate (idempotent).
                    Post.id.notin_(current_ids) if current_ids else True,
                )
                .order_by(Post.created.desc())
                .limit(slots)
            )
        )
        .scalars()
        .all()
    )
    for post_id in post_ids:
        await place_post(redis, str(user_id), post_id)
    return len(post_ids)


# --- rebuild ---------------------------------------------------------------

async def rebuild_from_pg(redis: Redis, session: AsyncSession) -> dict[str, int]:
    """Repopulate Redis distribution state from Postgres.

    - `channel:*` sets and `free_queue` from subscriptions.
    - `tokens:*` seeded from each user's lifetime `reviewed_count` (a proxy — actual
      spends are not tracked in PG).
    - Each subscriber's queue is backfilled with recent posts from their subscribed
      channels, so users who subscribed *before* Redis (empty queues) get content
      without having to re-subscribe.

    Idempotent: the backfill skips posts already queued, and token balances are set
    (not incremented). The `ops` list is not touched. Note: because tokens are reset
    to `reviewed_count`, re-running discards any spends since the last rebuild — run
    it for reconciliation/onboarding, not routinely.
    """
    subs = (await session.execute(select(ChannelSubscription))).scalars().all()
    users = (await session.execute(select(User))).scalars().all()

    scripts = get_scripts(redis)
    for user in users:
        await scripts.ensure_free(
            keys=[keys.queue(str(user.id)), keys.FREE_QUEUE],
            args=[str(user.id), settings.FEED_QUEUE_MAX_SLOTS],
        )
        await redis.set(keys.tokens(str(user.id)), user.reviewed_count)

    backfilled = 0
    for sub in subs:
        await redis.sadd(keys.channel(sub.channel_id), str(sub.user_id))
        backfilled += await backfill_queue(
            redis, session, sub.user_id, sub.channel_id
        )

    return {
        "subscriptions": len(subs),
        "users": len(users),
        "backfilled": backfilled,
    }
