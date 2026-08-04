"""High-level async helpers over the Redis client for the feed algorithm.

Postgres stays the source of truth for posts/reviews/subscriptions; everything here
manipulates only distribution *state* (queues, sets, counters, the operation queue),
storing post_ids rather than content.
"""

import asyncio
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
from app.feed.pricing import compute_price
from app.feed.scripts import get_scripts
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.user import User

logger = logging.getLogger(__name__)

# `place_post` returns this instead of a queue length when the post was refused
# because the user has already had it (see FEED_EXCLUDE_SEEN).
PLACE_REFUSED = -1


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
    redis: Redis,
    post_id: int,
    channel_id: int,
    expires_at: float | None = None,
    author_id: str | None = None,
) -> None:
    """Append a fan-out operation for `post_id` to the operation stream.

    `expires_at` is carried only by ops coming back from `ops:retry`; it preserves the
    original retry deadline across the stream round-trip so re-parking cannot reset it
    (see `schedule_retry`). Freshly published/forwarded posts pass None and get a
    deadline on their first park.

    `author_id` lets the worker skip the post's own author without looking anything up —
    the whole cost of FEED_EXCLUDE_OWN_POSTS is this one extra stream field. Optional on
    purpose: ops written before this existed (still on the stream, or parked in
    `ops:retry` across the deploy) simply carry no author and are fanned out as before,
    exactly like `expires_at` degrades.
    """
    fields = {"post_id": str(post_id), "channel_id": str(channel_id)}
    if expires_at is not None:
        fields["expires_at"] = str(expires_at)
    if author_id is not None:
        fields["author_id"] = author_id
    await redis.xadd(keys.STREAM, fields)


async def operation_queue_len(redis: Redis) -> int:
    """Outstanding operations in the stream (drives admission pricing).

    Because completed ops are XACK'd *and* XDEL'd (see worker), the stream retains only
    undelivered backlog plus in-flight (delivered-but-unacked) entries — a good
    proxy for congestion. Ops parked in `ops:retry` are not counted (they are not
    active work until re-added).
    """
    return await redis.xlen(keys.STREAM)


async def refresh_price_snapshot(redis: Redis, now: float | None = None) -> dict:
    """Unconditionally recompute the admission price from current congestion and
    publish it as the snapshot every reader/charge shares (see `get_price_snapshot`).

    `expires_at` (`computed_at + FEED_PRICE_REFRESH_SECONDS`) is a promise made to
    callers of `get_price_snapshot`/`GET /posts/economy`: the price will not change
    before then. This function keeps that promise for its *own* call, but does not by
    itself guard against an *earlier* caller's still-active promise — see
    `run_price_refresher`, which is why the periodic loop checks before calling this
    rather than calling it unconditionally.

    The Redis key TTL (FEED_PRICE_TTL_SECONDS, longer than the refresh interval) is a
    separate, purely internal safety net: if the refresher stalls entirely, the key
    itself expires and `get_price_snapshot` computes on demand rather than serving a
    snapshot that is stale forever.
    """
    if now is None:
        now = time.time()
    price = compute_price(await operation_queue_len(redis), settings)
    snapshot = {
        "price": price,
        "computed_at": now,
        "expires_at": now + settings.FEED_PRICE_REFRESH_SECONDS,
    }
    await redis.set(
        keys.PRICE_SNAPSHOT, json.dumps(snapshot), ex=settings.FEED_PRICE_TTL_SECONDS
    )
    return snapshot


async def get_price_snapshot(redis: Redis) -> dict:
    """The current shared admission price, as published by `refresh_price_snapshot`.

    Falls back to computing (and publishing) a fresh snapshot on a miss — cold start,
    a flushed Redis, or a stalled refresher — so a client is never refused a price;
    it just briefly reverts to an on-demand value until the timer catches up.
    """
    raw = await redis.get(keys.PRICE_SNAPSHOT)
    if raw is not None:
        return json.loads(raw)
    return await refresh_price_snapshot(redis)


async def maybe_refresh_price_snapshot(redis: Redis, now: float | None = None) -> dict:
    """Refresh the price snapshot, but only once its current `expires_at` has passed.

    Several processes may run `run_price_refresher` at once (see `app/factory.py`'s
    lifespan), each on its own uncoordinated schedule — one process's tick can land
    seconds before another's. Calling `refresh_price_snapshot` on every tick regardless
    let whichever process ticked first silently cut short a window every process had
    already quoted to clients as the price being guaranteed. Checking `expires_at`
    first makes every process agree "don't touch it before then", so the promise holds
    no matter how many processes are polling or how their schedules drift — the window
    can end up a little *longer* than FEED_PRICE_REFRESH_SECONDS (e.g. several
    processes' ticks all land a bit after expiry), never shorter.
    """
    if now is None:
        now = time.time()
    current = await get_price_snapshot(redis)
    if now >= current["expires_at"]:
        return await refresh_price_snapshot(redis, now)
    return current


async def run_price_refresher(redis: Redis) -> None:
    """Refresh the price snapshot roughly every FEED_PRICE_REFRESH_SECONDS until
    cancelled (see `maybe_refresh_price_snapshot` for why "roughly").

    Runs once immediately (before the first sleep) so a fresh deploy doesn't leave the
    snapshot missing for a full interval.
    """
    while True:
        try:
            await maybe_refresh_price_snapshot(redis)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("price snapshot refresh failed; continuing")
        try:
            await asyncio.sleep(settings.FEED_PRICE_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise


async def schedule_retry(
    redis: Redis,
    post_id: int,
    channel_id: int,
    delay: float | None = None,
    expires_at: float | None = None,
    author_id: str | None = None,
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

    `author_id` rides along the same way, so a parked op still knows to skip its author
    when it is eventually re-added to the stream.
    """
    if delay is None:
        delay = settings.FEED_RETRY_INTERVAL_SECONDS
    now = time.time()
    if expires_at is None:
        expires_at = now + settings.FEED_RETRY_MAX_AGE_SECONDS
    payload = json.dumps(
        {
            "post_id": post_id,
            "channel_id": channel_id,
            "expires_at": expires_at,
            "author_id": author_id,
        }
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
        await enqueue_operation(
            redis,
            op["post_id"],
            op["channel_id"],
            expires_at,
            op.get("author_id"),
        )
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

    Returns `PLACE_REFUSED` instead when the user has already had this post and
    FEED_EXCLUDE_SEEN is on. This is the choke point every delivery path goes through,
    so the guarantee holds for callers that never consulted `select_recipients` —
    callers must not count a refusal as a delivery.
    """
    return await get_scripts(redis).place(
        keys=[keys.queue(user_id), keys.FREE_QUEUE, keys.seen(post_id)],
        args=[
            post_id,
            user_id,
            settings.FEED_QUEUE_MAX_SLOTS,
            1 if settings.FEED_EXCLUDE_SEEN else 0,
            settings.FEED_SEEN_TTL_SECONDS,
        ],
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


async def is_queued(redis: Redis, user_id: str, post_id: int) -> bool:
    """Whether `post_id` currently sits in the user's queue, delivered and unreviewed."""
    return await redis.lpos(keys.queue(user_id), str(post_id)) is not None


# --- recipient selection ---------------------------------------------------

async def select_recipients(
    redis: Redis,
    channel_id: int,
    k: int,
    post_id: int | None = None,
    author_id: str | None = None,
) -> list[str]:
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

    `post_id`/`author_id` apply the delivery exclusions. Both are optional so an op that
    predates them still fans out. The order of the three filters is deliberate: drop the
    author first (pure local comparison, no round trip), then the free check, then the
    seen check against the already-shrunken free list — each SMISMEMBER is O(list), so
    filtering cheapest-first keeps the second one small.

    Note this is only the *efficient* path: `place_post` re-checks the seen set atomically
    and is what actually guarantees no re-delivery. Filtering here just stops the worker
    burning fan-out attempts on recipients that would be refused.
    """
    sample_size = k * settings.FEED_FANOUT_SAMPLE_MULTIPLIER
    candidates = await redis.srandmember(keys.channel(channel_id), sample_size)
    if not candidates:
        return []

    if author_id is not None and settings.FEED_EXCLUDE_OWN_POSTS:
        candidates = [uid for uid in candidates if uid != author_id]
        if not candidates:
            return []

    free_flags = await redis.smismember(keys.FREE_QUEUE, *candidates)
    free = [uid for uid, is_free in zip(candidates, free_flags) if is_free]

    if free and post_id is not None and settings.FEED_EXCLUDE_SEEN:
        seen_flags = await redis.smismember(keys.seen(post_id), *free)
        free = [uid for uid, was_seen in zip(free, seen_flags) if not was_seen]

    return free[:k]


async def has_eligible_recipient(
    redis: Redis, channel_id: int, post_id: int, author_id: str | None = None
) -> bool:
    """Could *any* subscriber still receive this post, ignoring queue capacity?

    Separates "undeliverable right now" from "undeliverable forever". Before exclusions
    existed every subscriber was always a valid target, so an empty recipient list only
    ever meant full queues, and parking for retry was always the right answer. Exclusions
    make a post able to genuinely run out of audience — and parking one of those would
    cycle it through the stream every FEED_RETRY_INTERVAL_SECONDS until
    FEED_RETRY_MAX_AGE_SECONDS: ~21k pointless fan-out attempts over 5 days per saturated
    post, each one inflating XLEN and therefore the admission price everyone pays.

    Cheap gate first. Exhaustion requires the seen set to cover every subscriber bar at
    most the author, so `seen + 1 < subscribers` rules it out with two O(1) SCARDs — the
    common "everyone is merely full" case never pays more than that. Only when the gate
    passes do we spend the O(channel) SDIFF, and a confirmed exhaustion abandons the op,
    so that scan does not recur for the same post.

    A channel with *no* subscribers is deliberately treated as still-eligible. Subscribing
    pulls no history, so a parked op is the only way a brand-new channel's backlog ever
    reaches its first subscriber (see tests/feed/test_worker.py::TestBacklogDelivery).
    Exhaustion is the narrower claim: subscribers exist, and every one of them is already
    excluded.
    """
    if not (settings.FEED_EXCLUDE_SEEN or settings.FEED_EXCLUDE_OWN_POSTS):
        return True

    channel_key = keys.channel(channel_id)
    subscribers = await redis.scard(channel_key)
    if subscribers == 0:
        return True

    seen_count = (
        await redis.scard(keys.seen(post_id)) if settings.FEED_EXCLUDE_SEEN else 0
    )
    # +1 covers the author, who may be a subscriber but is never in the seen set
    # (they are excluded before delivery, so they never get placed).
    if seen_count + 1 < subscribers:
        return True

    if settings.FEED_EXCLUDE_SEEN:
        remaining = await redis.sdiff([channel_key, keys.seen(post_id)])
    else:
        remaining = await redis.smembers(channel_key)
    if author_id is not None and settings.FEED_EXCLUDE_OWN_POSTS:
        remaining.discard(author_id)
    return bool(remaining)


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
    filters = [
        Post.channel_id == channel_id,
        Post.id.notin_(already_reviewed),
        # Skip posts already queued so re-runs don't duplicate (idempotent).
        Post.id.notin_(current_ids) if current_ids else True,
    ]
    if settings.FEED_EXCLUDE_OWN_POSTS:
        # The fan-out path skips the author via the stream entry, which this path has
        # no equivalent of — filter in SQL instead, or a rebuild would hand authors
        # back their own posts that live delivery had correctly withheld.
        filters.append(Post.author_id != user_id)
    post_ids = (
        (
            await session.execute(
                select(Post.id)
                .filter(*filters)
                .order_by(Post.created.desc())
                .limit(slots)
            )
        )
        .scalars()
        .all()
    )
    # A refusal (already delivered before) is not a backfill — don't count it.
    placed = 0
    for post_id in post_ids:
        if await place_post(redis, str(user_id), post_id) != PLACE_REFUSED:
            placed += 1
    return placed


# --- rebuild ---------------------------------------------------------------

async def seed_seen_from_reviews(redis: Redis, session: AsyncSession) -> int:
    """Rebuild the `seen:*` sets from the `post_reviews` table. Returns rows seeded.

    Needed twice over. On the deploy that introduces FEED_EXCLUDE_SEEN the sets start
    empty, so without this every user gets one last round of posts they had already
    reviewed. And after any Redis loss, a rebuild that skipped this would silently
    re-open re-delivery for every post still in circulation.

    Recovers reviews only — a post sitting *delivered but unreviewed* in someone's queue
    leaves no trace in Postgres. That self-heals: `backfill_queue` re-places those posts
    and `place_post` re-marks them, which is why `rebuild_from_pg` runs this first.
    """
    if not settings.FEED_EXCLUDE_SEEN:
        return 0

    rows = (
        await session.execute(select(PostReview.post_id, PostReview.user_id))
    ).all()
    by_post: dict[int, list[str]] = {}
    for post_id, user_id in rows:
        by_post.setdefault(post_id, []).append(str(user_id))
    for post_id, user_ids in by_post.items():
        await redis.sadd(keys.seen(post_id), *user_ids)
        await redis.expire(keys.seen(post_id), settings.FEED_SEEN_TTL_SECONDS)
    return len(rows)


async def rebuild_from_pg(redis: Redis, session: AsyncSession) -> dict[str, int]:
    """Repopulate Redis distribution state from Postgres.

    - `channel:*` sets and `free_queue` from subscriptions.
    - `tokens:*` seeded from `FEED_STARTING_TOKENS + reviewed_count` (a proxy — actual
      spends are not tracked in PG, and reviewed_count is itself a proxy for earned
      tokens, but the starting grant *is* durable policy, not something to lose in
      a rebuild).
    - `seen:*` from `post_reviews`, so the re-delivery guard survives a rebuild.
    - Each subscriber's queue is backfilled with recent posts from their subscribed
      channels, so users who subscribed *before* Redis (empty queues) get content
      without having to re-subscribe.

    Idempotent: the backfill skips posts already queued, seen-set writes are SADDs, and
    token balances are set (not incremented). The operation stream and `ops:retry` are
    not touched. Note: because tokens are reset to `FEED_STARTING_TOKENS +
    reviewed_count`, re-running discards any spends since the last rebuild — run it
    for reconciliation/onboarding, not routinely.
    """
    subs = (await session.execute(select(ChannelSubscription))).scalars().all()
    users = (await session.execute(select(User))).scalars().all()

    # Before the backfill: it places posts, and placement consults these sets.
    seen_seeded = await seed_seen_from_reviews(redis, session)

    scripts = get_scripts(redis)
    for user in users:
        await scripts.ensure_free(
            keys=[keys.queue(str(user.id)), keys.FREE_QUEUE],
            args=[str(user.id), settings.FEED_QUEUE_MAX_SLOTS],
        )
        await redis.set(
            keys.tokens(str(user.id)),
            settings.FEED_STARTING_TOKENS + user.reviewed_count,
        )

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
        "seen_seeded": seen_seeded,
    }
