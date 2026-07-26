"""High-level async helpers over the Redis client for the feed algorithm.

Postgres stays the source of truth for posts/reviews/subscriptions; everything here
manipulates only distribution *state* (queues, sets, counters, the operation queue),
storing post_ids rather than content.
"""

import json
import logging
import time
import uuid
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.feed import keys
from app.feed.scripts import get_scripts
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.user import User

logger = logging.getLogger(__name__)


# --- operation stream ------------------------------------------------------

async def ensure_group(redis: Redis) -> None:
    """Idempotently create the consumer group (and the stream) if missing.

    Created at id ``0`` (not ``$``) so any entries added before the group existed are
    still delivered — the group must never skip work. Safe to call repeatedly; a
    second call raises BUSYGROUP, which we swallow. Callers that hit NOGROUP (the
    group/stream was flushed) can call this and retry.
    """
    try:
        await redis.xgroup_create(keys.STREAM, keys.STREAM_GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue_operation(
    redis: Redis, post_id: int, channel_id: int, expires_at: float | None = None
) -> None:
    """Append a fan-out operation for `post_id` to the operation stream.

    `expires_at` is carried only by ops coming back from `ops:retry`; it preserves the
    original retry deadline across the stream round-trip so re-parking cannot reset it
    (see `schedule_retry`). Freshly published/forwarded posts pass None and get a
    deadline on their first park.
    """
    fields = {"post_id": str(post_id), "channel_id": str(channel_id)}
    if expires_at is not None:
        fields["expires_at"] = str(expires_at)
    await redis.xadd(keys.STREAM, fields)


async def operation_queue_len(redis: Redis) -> int:
    """Outstanding operations in the stream (drives admission pricing).

    Because completed ops are XACK'd *and* XDEL'd (see worker), the stream retains only
    undelivered backlog plus in-flight (delivered-but-unacked) entries — a good
    proxy for congestion. Ops parked in `ops:retry` are not counted (they are not
    active work until re-added).
    """
    return await redis.xlen(keys.STREAM)


async def schedule_retry(
    redis: Redis,
    post_id: int,
    channel_id: int,
    delay: float | None = None,
    expires_at: float | None = None,
) -> None:
    """Park an undeliverable operation in `ops:retry`, due after `delay` seconds
    (defaults to `FEED_RETRY_INTERVAL_SECONDS`).

    Held in a sorted set rather than re-added immediately, so a channel with no
    free recipients doesn't spin the consumer in a tight retry loop. The member
    is prefixed with a random id so two retries for the same post/channel pair
    (e.g. a create and a later forward, both stalled) never collide as a single
    sorted-set entry.

    `expires_at` is the absolute deadline past which the op is abandoned rather than
    retried again. It is set once, on the first park (`now + FEED_RETRY_MAX_AGE_SECONDS`),
    and thereafter passed back in by the caller so repeated parking cannot extend it.
    """
    if delay is None:
        delay = settings.FEED_RETRY_INTERVAL_SECONDS
    now = time.time()
    if expires_at is None:
        expires_at = now + settings.FEED_RETRY_MAX_AGE_SECONDS
    payload = json.dumps(
        {"post_id": post_id, "channel_id": channel_id, "expires_at": expires_at}
    )
    member = f"{uuid4().hex}:{payload}"
    await redis.zadd(keys.OPS_RETRY, {member: now + delay})


async def reschedule_due_retries(redis: Redis, now: float | None = None) -> int:
    """Re-add due operations from `ops:retry` to the operation stream.

    Streams are append-only, so a re-added retry goes to the tail (a new id) rather
    than jumping ahead of newer work as it did with the old list — a cosmetic ordering
    change, not a correctness one.

    An op past its `expires_at` deadline is dropped instead of re-added: a channel that
    never gains a free subscriber would otherwise cycle its posts through the stream
    indefinitely. Returns the number rescheduled (expired ops are not counted).
    """
    if now is None:
        now = time.time()
    due = await redis.zrangebyscore(keys.OPS_RETRY, "-inf", now)
    rescheduled = 0
    for member in due:
        _, _, payload = member.partition(":")
        op = json.loads(payload)
        expires_at = op.get("expires_at")
        if expires_at is None:
            # Parked before deadlines existed. Stamp one from now: the upgrade neither
            # discards a live backlog nor leaves ops that can never expire.
            expires_at = now + settings.FEED_RETRY_MAX_AGE_SECONDS
        elif expires_at <= now:
            await redis.zrem(keys.OPS_RETRY, member)
            logger.info(
                "feed op abandoned after %ss of retries: post_id=%s channel_id=%s",
                settings.FEED_RETRY_MAX_AGE_SECONDS,
                op["post_id"],
                op["channel_id"],
            )
            continue
        # XADD before ZREM: if the process dies in between, the op is merely duplicated
        # (already-tolerated, see claim_from_queue/review_post's IntegrityError
        # handling) rather than silently lost.
        await enqueue_operation(redis, op["post_id"], op["channel_id"], expires_at)
        await redis.zrem(keys.OPS_RETRY, member)
        rescheduled += 1
    return rescheduled


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

    Idempotent: a post already in the queue is not pushed again, so the worker's
    fan-out and `backfill_queue` can both target the same (user, post) without
    duplicating it. Returns the queue length after the push.
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

    Sample-then-filter, not intersect: `SRANDMEMBER` a bounded random sample of the
    channel's subscribers (`k * FEED_FANOUT_SAMPLE_MULTIPLIER`), then keep those in
    `free_queue` via a single `SMISMEMBER`. Both calls are O(sample), independent of
    channel size — unlike a per-op `SINTERSTORE(channel, free_queue)`, which scans a
    set proportional to the whole channel (or the global free set) and blocks the
    single-threaded server for that long on *every* operation.

    Trade-off: for a channel large enough that the sample is a strict subset, this is
    probabilistic — if free subscribers are rare, the sample may miss them and the op
    is parked for retry (correct: the channel is genuinely congested). For channels at
    or below the sample size the whole set is drawn, so selection is exact, matching
    the old behaviour. Oversampling (the multiplier) keeps the miss rate low until a
    channel is heavily saturated.
    """
    sample_size = k * settings.FEED_FANOUT_SAMPLE_MULTIPLIER
    candidates = await redis.srandmember(keys.channel(channel_id), sample_size)
    if not candidates:
        return []
    free_flags = await redis.smismember(keys.FREE_QUEUE, *candidates)
    free = [uid for uid, is_free in zip(candidates, free_flags) if is_free]
    return free[:k]


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
    """Fill a subscriber's free queue slots with recent un-reviewed channel posts.

    Reconciliation only — used by `rebuild_from_pg`, not by the subscribe endpoint.
    Live distribution is push-based (operations → worker fan-out) and that is the sole
    delivery path for a new subscriber: they receive posts published from then on, plus
    any parked in `ops:retry`. Calling this on subscribe would add a second, competing
    delivery path for the same post. Returns the number of posts backfilled.
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
    (not incremented). The operation stream and `ops:retry` are not touched. Note:
    because tokens are reset to `reviewed_count`, re-running discards any spends since
    the last rebuild — run it for reconciliation/onboarding, not routinely.
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
