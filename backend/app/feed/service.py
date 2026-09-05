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
from collections.abc import Sequence
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.feed import keys
from app.feed.pricing import channel_price as compute_channel_price
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


async def _add_to_stream(
    redis: Redis,
    post_id: int,
    channel_id: int,
    expires_at: float | None = None,
    author_id: str | None = None,
) -> None:
    """Append a fan-out operation to the stream, and *only* that.

    Split from `enqueue_operation` because re-adding a parked op is not new work: the
    channel's outstanding count was incremented when the op was first minted and has
    stayed up through the whole park/retry cycle, so counting it again here would
    inflate a stalled channel's price once per retry.

    `expires_at` is carried only by ops coming back from `ops:retry`; it preserves the
    original retry deadline across the stream round-trip so re-parking cannot reset it
    (see `schedule_retry`).

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


async def enqueue_operation(
    redis: Redis,
    post_id: int,
    channel_id: int,
    author_id: str | None = None,
) -> None:
    """Mint a *new* fan-out operation for `post_id` (a publish or a forward).

    Also counts it against the channel (see keys.OPS_OUTSTANDING), which is what makes
    per-channel pricing possible: this is where work enters the system, and
    `retire_operation` is the only place it leaves. Ops re-added from `ops:retry` go
    through `_add_to_stream` instead, which skips the count.
    """
    await _add_to_stream(redis, post_id, channel_id, author_id=author_id)
    await redis.hincrby(keys.OPS_OUTSTANDING, str(channel_id), 1)


async def retire_operation(redis: Redis, channel_id: int) -> int:
    """Drop one outstanding op from the channel's count. Returns the new count.

    Called at a *terminal* outcome only — the post reached a recipient, or was abandoned
    — never when an op is merely parked for retry, which leaves it outstanding by
    design. Floors at zero rather than going negative; see the `retire` script for why
    that matters.
    """
    return int(
        await get_scripts(redis).retire(
            keys=[keys.OPS_OUTSTANDING], args=[str(channel_id)]
        )
    )


async def operation_queue_len(redis: Redis) -> int:
    """Outstanding operations in the stream (drives admission pricing).

    Because completed ops are XACK'd *and* XDEL'd (see worker), the stream retains only
    undelivered backlog plus in-flight (delivered-but-unacked) entries — a good
    proxy for congestion. Ops parked in `ops:retry` are not counted (they are not
    active work until re-added).
    """
    return await redis.xlen(keys.STREAM)


async def outstanding_ops_total(redis: Redis) -> int:
    """Outstanding ops across every channel — the numerator's global counterpart.

    Summed from the per-channel hash rather than read off `XLEN`, so both sides of the
    per-channel ratio are measured the same way: this total includes parked ops, and it
    is the exact sum of the values `channel_factor` divides by. O(channels), paid once
    per refresher tick.
    """
    values = await redis.hvals(keys.OPS_OUTSTANDING)
    return sum(max(0, int(v)) for v in values)


async def channel_outstanding_ops(
    redis: Redis, channel_ids: Sequence[int]
) -> dict[int, int]:
    """Outstanding op count per channel, in one round trip. Missing ⇒ 0."""
    if not channel_ids:
        return {}
    raw = await redis.hmget(keys.OPS_OUTSTANDING, [str(c) for c in channel_ids])
    return {
        cid: max(0, int(v)) if v is not None else 0
        for cid, v in zip(channel_ids, raw, strict=True)
    }


async def subscription_total(redis: Redis) -> int:
    """Total channel subscriptions (see keys.SUBS_TOTAL). Clamped at 0.

    A running counter, so it can drift below the truth if Redis loses the key while
    keeping the channel sets. It only ever scales a price within the channel band, and
    `rebuild_from_pg` resets it from Postgres.
    """
    raw = await redis.get(keys.SUBS_TOTAL)
    return max(0, int(raw)) if raw is not None else 0


async def _read_price_snapshot(redis: Redis) -> dict | None:
    """Raw read of the published snapshot, or None on a miss. No fallback compute —
    `refresh_price_snapshot` uses this to read the *previous* price without risking
    recursion back into itself via `get_price_snapshot`.
    """
    raw = await redis.get(keys.PRICE_SNAPSHOT)
    return json.loads(raw) if raw is not None else None


async def refresh_price_snapshot(redis: Redis, now: float | None = None) -> dict:
    """Unconditionally step the admission price from current congestion and publish
    it as the snapshot every reader/charge shares (see `get_price_snapshot`).

    The price is stateful (see `pricing.compute_price`): each call nudges it by 1
    from whatever it was published as last, defaulting to FEED_PRICE_MIN when there
    is no previous snapshot (cold start) — the same starting point a fresh deploy
    with an empty queue would settle on anyway.

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
    previous = await _read_price_snapshot(redis)
    prev_price = previous["price"] if previous is not None else settings.FEED_PRICE_MIN
    ops_len = await operation_queue_len(redis)
    active_users = await active_user_count(redis, now)
    price = compute_price(prev_price, ops_len, active_users, settings)
    snapshot = {
        "price": price,
        "computed_at": now,
        "expires_at": now + settings.FEED_PRICE_REFRESH_SECONDS,
        # Carried on the snapshot so a per-channel price (`channel_prices`) needs only
        # the channel's own two numbers to derive itself — and so every channel priced
        # within this window is scaled against the same global ratio, rather than each
        # re-reading totals that moved in between.
        "ops_total": await outstanding_ops_total(redis),
        "subs_total": await subscription_total(redis),
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
    snapshot = await _read_price_snapshot(redis)
    if snapshot is not None:
        return snapshot
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


async def channel_prices(
    redis: Redis, channel_ids: Sequence[int], now: float | None = None
) -> dict[int, int]:
    """Admission price for each of `channel_ids`, frozen for the current price window.

    Each price is the global snapshot's price scaled by that channel's own congestion
    (see `pricing.channel_factor`), then cached under `keys.channel_price` stamped with
    the *snapshot's* `expires_at`. So a channel's price is guaranteed for exactly as
    long as the base price it came from, and both roll over on the same tick — which is
    what lets a channel list quote a number that `create_post` will still charge.

    Computed lazily per channel rather than for every channel on the refresher tick: the
    refresher has no way to enumerate channels without either a Postgres session or a
    `SCAN` over the keyspace, and only channels somebody actually looks at need a price.

    At most three round trips whatever the page size — the cached reads, then the two
    counters for whatever missed, then the write-back.
    """
    if not channel_ids:
        return {}
    if now is None:
        now = time.time()

    wanted = list(dict.fromkeys(channel_ids))
    cached = await redis.mget([keys.channel_price(c) for c in wanted])

    prices: dict[int, int] = {}
    missing: list[int] = []
    for channel_id, raw in zip(wanted, cached, strict=True):
        entry = json.loads(raw) if raw is not None else None
        if entry is not None and now < entry["expires_at"]:
            prices[channel_id] = entry["price"]
        else:
            missing.append(channel_id)
    if not missing:
        return prices

    snapshot = await get_price_snapshot(redis)
    # Absent from a snapshot published before per-channel pricing existed, which can
    # still be live for up to FEED_PRICE_TTL_SECONDS after the deploy. Zeroes make
    # `channel_factor` neutral, so those channels price at the flat global rate for one
    # window instead of raising.
    ops_total = snapshot.get("ops_total", 0)
    subs_total = snapshot.get("subs_total", 0)
    ops_by_channel = await channel_outstanding_ops(redis, missing)

    pipe = redis.pipeline(transaction=False)
    for channel_id in missing:
        pipe.scard(keys.channel(channel_id))
    subscriber_counts = await pipe.execute()

    pipe = redis.pipeline(transaction=False)
    for channel_id, subscribers in zip(missing, subscriber_counts, strict=True):
        price = compute_channel_price(
            snapshot["price"],
            ops_by_channel[channel_id],
            subscribers,
            ops_total,
            subs_total,
            settings,
        )
        prices[channel_id] = price
        pipe.set(
            keys.channel_price(channel_id),
            json.dumps({"price": price, "expires_at": snapshot["expires_at"]}),
            ex=settings.FEED_PRICE_TTL_SECONDS,
        )
    await pipe.execute()
    return prices


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
            # Terminal: this op will never be delivered, so it stops counting against
            # the channel's price.
            await retire_operation(redis, op["channel_id"])
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
        #
        # `_add_to_stream`, not `enqueue_operation`: this op is already counted against
        # its channel and has been throughout its time parked here.
        await _add_to_stream(
            redis,
            op["post_id"],
            op["channel_id"],
            expires_at,
            op.get("author_id"),
        )
        await redis.zrem(keys.OPS_RETRY, member)
        rescheduled += 1
    return rescheduled


# --- presence ----------------------------------------------------------------

async def mark_active(redis: Redis, user_id: str, now: float | None = None) -> None:
    """Record `user_id` as active right now (see keys.ACTIVE_USERS).

    A plain ZADD: the sliding window lives entirely in how the score is read back
    (`active_user_count`), so marking active twice just overwrites the score — the
    window naturally "resets" on every call without any separate TTL bookkeeping.
    """
    if now is None:
        now = time.time()
    await redis.zadd(keys.ACTIVE_USERS, {user_id: now})


async def active_user_count(redis: Redis, now: float | None = None) -> int:
    """Users marked active within the trailing ACTIVE_USER_WINDOW_SECONDS.

    Stale entries (older than the window) are trimmed opportunistically on read
    rather than via TTL (Redis cannot expire individual set members) — cheap since
    it's the same O(log N) sorted-set operation family as the ZCOUNT itself, and
    keeps the set from growing unbounded with users who never come back.
    """
    if now is None:
        now = time.time()
    cutoff = now - settings.ACTIVE_USER_WINDOW_SECONDS
    await redis.zremrangebyscore(keys.ACTIVE_USERS, "-inf", cutoff)
    return await redis.zcount(keys.ACTIVE_USERS, cutoff, "+inf")


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
    FEED_RETRY_MAX_AGE_SECONDS: ~43k pointless fan-out attempts over 10 days per
    saturated post, each one inflating XLEN and therefore the admission price everyone
    pays. That arithmetic is why this gate got *more* valuable when the deadline was
    doubled, not less.

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
    """Reflect a subscription: add to the channel set, ensure reachable via free_queue.

    The running subscription total (keys.SUBS_TOTAL) moves only when the SADD actually
    added — subscribing twice must not inflate it, and the endpoint is deliberately
    idempotent.
    """
    added = await redis.sadd(keys.channel(channel_id), user_id)
    if added:
        await redis.incr(keys.SUBS_TOTAL)
    await get_scripts(redis).ensure_free(
        keys=[keys.queue(user_id), keys.FREE_QUEUE],
        args=[user_id, settings.FEED_QUEUE_MAX_SLOTS],
    )


async def sync_unsubscribe(redis: Redis, user_id: str, channel_id: int) -> None:
    """Reflect an unsubscription: remove the user from the channel set."""
    removed = await redis.srem(keys.channel(channel_id), user_id)
    if removed:
        await redis.decr(keys.SUBS_TOTAL)


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


async def reseed_outstanding_ops(redis: Redis) -> int:
    """Recount the per-channel outstanding-op counters from the ops actually in flight
    (the stream plus `ops:retry`). Returns the total counted.

    These counters are incremented and decremented by three different processes and
    survive nothing, so unlike the rest of the feed's Redis state they can be *wrong*
    rather than merely absent — a crash between `XADD` and the increment, a flushed
    hash, an op processed twice. Recounting is cheap because both structures are already
    self-trimming to outstanding work, and it is the only way back to the truth.

    An op parked and not yet XDEL'd from the stream is counted twice here. That window is
    a few instructions wide, and this is a price input, not a delivery decision.
    """
    counts: dict[str, int] = {}
    for _entry_id, fields in await redis.xrange(keys.STREAM):
        channel_id = fields.get("channel_id")
        if channel_id is not None:
            counts[channel_id] = counts.get(channel_id, 0) + 1
    for member in await redis.zrange(keys.OPS_RETRY, 0, -1):
        _, _, payload = member.partition(":")
        channel_id = str(json.loads(payload)["channel_id"])
        counts[channel_id] = counts.get(channel_id, 0) + 1

    await redis.delete(keys.OPS_OUTSTANDING)
    if counts:
        await redis.hset(keys.OPS_OUTSTANDING, mapping=counts)
    return sum(counts.values())


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
    - `subs:total` and the per-channel outstanding-op counters, the two running counters
      behind per-channel pricing — the only state here that can drift rather than simply
      be missing, so a rebuild is also their reconciliation.

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

    # Set from Postgres rather than summing what SADD just added: a rebuild over sets
    # that already held most members would otherwise count only the new ones.
    await redis.set(keys.SUBS_TOTAL, len(subs))
    outstanding = await reseed_outstanding_ops(redis)

    return {
        "subscriptions": len(subs),
        "users": len(users),
        "backfilled": backfilled,
        "seen_seeded": seen_seeded,
        "outstanding_ops": outstanding,
    }
