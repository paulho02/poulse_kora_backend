"""High-level async helpers over the Redis client for the feed algorithm.

Postgres stays the source of truth for posts/reviews/subscriptions; everything here
manipulates only distribution *state* (queues, sets, counters, the operation queue),
storing post_ids rather than content.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Sequence
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import languages as languages_module
from app.core.config import settings
from app.core.languages import UNSPECIFIED
from app.core.logger import get_logger
from app.feed import keys
from app.feed.pricing import compute_price
from app.feed.pricing import route_price as compute_route_price
from app.feed.scripts import get_scripts
from app.models.channel_subscription import ChannelSubscription
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.user import User

log = get_logger(__name__)

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
    language: str,
    expires_at: float | None = None,
    author_id: str | None = None,
    fanout: int | None = None,
) -> None:
    """Append a fan-out operation to the stream, and *only* that.

    Split from `enqueue_operation` because re-adding a parked op is not new work: the
    route's outstanding count was incremented when the op was first minted and has
    stayed up through the whole park/retry cycle, so counting it again here would
    inflate a stalled route's price once per retry.

    `language` is the other half of the routing key (see keys.audience). Carried on the
    entry rather than looked up, for the same reason `author_id` is: the worker never
    opens a Postgres session, so everything fan-out needs to decide *where* a post goes
    has to travel with the operation.

    `expires_at` is carried only by ops coming back from `ops:retry`; it preserves the
    original retry deadline across the stream round-trip so re-parking cannot reset it
    (see `schedule_retry`).

    `author_id` lets the worker skip the post's own author without looking anything up —
    the whole cost of FEED_EXCLUDE_OWN_POSTS is this one extra stream field. Optional on
    purpose: ops written before this existed (still on the stream, or parked in
    `ops:retry` across the deploy) simply carry no author and are fanned out as before,
    exactly like `expires_at` degrades.

    `fanout` is how many recipients this particular operation is worth — the forwarder's
    Reviewer Trust band, resolved once at forward time (see app/core/trust.py). It has to
    travel here for the same reason the other two do: the worker never opens a Postgres
    session, so it cannot look up whose forward this was, let alone score them. None
    means "whatever FEED_FANOUT says", which is both the disabled-feature path and the
    degradation path for ops minted before this field existed.

    Resolving it once, at forward time, rather than when the op is finally delivered is
    deliberate. A parked op keeps the fan-out it was minted with, so a reader's verdict
    is worth what their judgement was worth *when they made it* - not what it has drifted
    to over the ten days the post spent looking for an audience.
    """
    fields = {
        "post_id": str(post_id),
        "channel_id": str(channel_id),
        "language": language,
    }
    if expires_at is not None:
        fields["expires_at"] = str(expires_at)
    if author_id is not None:
        fields["author_id"] = author_id
    if fanout is not None:
        fields["fanout"] = str(fanout)
    await redis.xadd(keys.STREAM, fields)


async def enqueue_operation(
    redis: Redis,
    post_id: int,
    channel_id: int,
    language: str,
    author_id: str | None = None,
    fanout: int | None = None,
) -> None:
    """Mint a *new* fan-out operation for `post_id` (a publish or a forward).

    Also counts it against the route (see keys.OPS_OUTSTANDING), which is what makes
    per-route pricing possible: this is where work enters the system, and
    `retire_operation` is the only place it leaves. Ops re-added from `ops:retry` go
    through `_add_to_stream` instead, which skips the count.

    `fanout` narrows or widens this one operation's reach (see `_add_to_stream`). Only
    the *forward* path passes it: an original post's reach is what its author paid the
    admission price for, and discounting or inflating that would be creator trust, a
    different feature with a different economy.
    """
    await _add_to_stream(
        redis, post_id, channel_id, language, author_id=author_id, fanout=fanout
    )
    await redis.hincrby(keys.OPS_OUTSTANDING, keys.route(channel_id, language), 1)


async def retire_operation(redis: Redis, channel_id: int, language: str) -> int:
    """Drop one outstanding op from the route's count. Returns the new count.

    Called at a *terminal* outcome only — the post reached a recipient, or was abandoned
    — never when an op is merely parked for retry, which leaves it outstanding by
    design. Floors at zero rather than going negative; see the `retire` script for why
    that matters.
    """
    return int(
        await get_scripts(redis).retire(
            keys=[keys.OPS_OUTSTANDING], args=[keys.route(channel_id, language)]
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


async def route_outstanding_ops(
    redis: Redis, routes: Sequence[tuple[int, str]]
) -> dict[tuple[int, str], int]:
    """Outstanding op count per (channel, language) route, in one round trip.
    Missing ⇒ 0."""
    if not routes:
        return {}
    raw = await redis.hmget(
        keys.OPS_OUTSTANDING, [keys.route(cid, lang) for cid, lang in routes]
    )
    return {
        route: max(0, int(v)) if v is not None else 0
        for route, v in zip(routes, raw, strict=True)
    }


async def subscription_total(redis: Redis) -> int:
    """Total audience memberships (see keys.SUBS_TOTAL). Clamped at 0.

    A running counter, so it can drift below the truth if Redis loses the key while
    keeping the audience sets. It only ever scales a price within the channel band, and
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
    # Only when it actually moves. The refresher runs every
    # FEED_PRICE_REFRESH_SECONDS and most ticks change nothing (the deadband
    # exists precisely to make that true), so logging every tick would be a
    # minute-by-minute heartbeat saying "still 3". Logging the *transitions*
    # gives the price history of the economy for free: the inputs are on the line,
    # so a complaint that posting got expensive is answerable to the minute.
    if previous is None or previous["price"] != price:
        log.info(
            "feed.price_changed",
            price=price,
            # Absent rather than equal to `price` when there was no previous
            # snapshot at all (a cold start or a flushed Redis) - that is a
            # different event from a price that moved, and the missing field is
            # what says so.
            previous_price=previous["price"] if previous is not None else None,
            ops_queue=ops_len,
            active_users=active_users,
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


async def route_prices(
    redis: Redis, routes: Sequence[tuple[int, str]], now: float | None = None
) -> dict[tuple[int, str], int]:
    """Admission price for each (channel, language) route, frozen for the current
    price window.

    Each price is the global snapshot's price scaled by that route's own congestion
    (see `pricing.route_factor`), then cached under `keys.route_price` stamped with the
    *snapshot's* `expires_at`. So a route's price is guaranteed for exactly as long as
    the base price it came from, and both roll over on the same tick — which is what
    lets a channel list quote a number that `create_post` will still charge.

    Computed lazily per route rather than for every route on the refresher tick: the
    refresher has no way to enumerate channels without either a Postgres session or a
    `SCAN` over the keyspace, and only routes somebody actually looks at need a price.
    Language multiplied the number of prices by CONTENT_LANGUAGES + 1 without changing
    that arithmetic — and it is why the deployment-wide range is *observed* from these
    computations rather than calculated from an enumeration (see keys.PRICE_RANGE).

    Three batched round trips whatever the page size — the cached reads, the subscriber
    counts for whatever missed, and the write-back — so pricing every route of six
    channels costs the same trips as pricing one. `observe_prices` then adds one call
    per channel plus one global, but only for routes that actually missed the cache,
    which is once per route per window: the steady state, where every route on the page
    is cached, is a single MGET.
    """
    if not routes:
        return {}
    if now is None:
        now = time.time()

    wanted = list(dict.fromkeys(routes))
    cached = await redis.mget([keys.route_price(c, lang) for c, lang in wanted])

    prices: dict[tuple[int, str], int] = {}
    missing: list[tuple[int, str]] = []
    for route, raw in zip(wanted, cached, strict=True):
        entry = json.loads(raw) if raw is not None else None
        if entry is not None and now < entry["expires_at"]:
            prices[route] = entry["price"]
        else:
            missing.append(route)
    if not missing:
        return prices

    snapshot = await get_price_snapshot(redis)
    # Absent from a snapshot published before per-route pricing existed, which can
    # still be live for up to FEED_PRICE_TTL_SECONDS after the deploy. Zeroes make
    # `route_factor` neutral, so those routes price at the flat global rate for one
    # window instead of raising.
    ops_total = snapshot.get("ops_total", 0)
    subs_total = snapshot.get("subs_total", 0)
    ops_by_route = await route_outstanding_ops(redis, missing)

    pipe = redis.pipeline(transaction=False)
    for channel_id, language in missing:
        pipe.scard(keys.audience(channel_id, language))
    subscriber_counts = await pipe.execute()

    pipe = redis.pipeline(transaction=False)
    computed: dict[int, list[int]] = {}
    for route, subscribers in zip(missing, subscriber_counts, strict=True):
        channel_id, language = route
        price = compute_route_price(
            snapshot["price"],
            ops_by_route[route],
            subscribers,
            ops_total,
            subs_total,
            settings,
        )
        prices[route] = price
        computed.setdefault(channel_id, []).append(price)
        pipe.set(
            keys.route_price(channel_id, language),
            json.dumps({"price": price, "expires_at": snapshot["expires_at"]}),
            ex=settings.FEED_PRICE_TTL_SECONDS,
        )
    await pipe.execute()

    await observe_prices(
        redis, computed, snapshot["expires_at"], base=snapshot["price"]
    )
    return prices


async def observe_prices(
    redis: Redis,
    prices_by_channel: dict[int, list[int]],
    expires_at: float,
    *,
    base: int,
) -> None:
    """Fold freshly computed route prices into the observed ranges (keys.PRICE_RANGE).

    Only prices that were actually *computed* are observed, never ones served from
    cache. A route's price is computed once per window, so that first computation is
    the only observation there is to make; re-observing a cached read would add round
    trips to every later request to re-assert a fact already recorded.

    Only the extremes of each group are sent. The script folds a list, and the min and
    max of a list widen a range by exactly as much as every element would.

    The window is the snapshot's `expires_at` truncated to a whole second — see the
    `price_range` script for why it has to be an integer compared as a string. `base`
    is that window's shared price, recorded so a reader in a *later* window can rescale
    this spread instead of throwing it away (see `read_price_range`).

    The key outlives its own window by a whole window, because the window after it
    reads it as a fallback: a TTL of only FEED_PRICE_TTL_SECONDS would drop the spread
    exactly when nobody had looked for a while, which is the case the fallback is for.
    """
    if not prices_by_channel:
        return
    window = str(int(expires_at))
    ttl = settings.FEED_PRICE_TTL_SECONDS + settings.FEED_PRICE_REFRESH_SECONDS
    price_range = get_scripts(redis).price_range
    everything = [p for prices in prices_by_channel.values() for p in prices]
    await price_range(
        keys=[keys.PRICE_RANGE],
        args=[window, ttl, base, min(everything), max(everything)],
    )
    for channel_id, prices in prices_by_channel.items():
        await price_range(
            keys=[keys.channel_price_range(channel_id)],
            args=[window, ttl, base, min(prices), max(prices)],
        )


def _rescale_range(
    low: int, high: int, base: int, current_base: int
) -> tuple[int, int]:
    """Carry a spread observed against `base` over to `current_base`.

    A route price is the window's base price times that route's own congestion factor,
    and the factor is the half that barely moves from one window to the next — traffic
    does not reshape itself in a minute, while the base price is by definition the half
    that just changed. So what carries over is the *shape* of the spread, rescaled onto
    the new base.

    Widened to include `current_base` too, so a carried range can never be narrower or
    further from the truth than the base-price-at-both-ends fallback it replaces: a
    route with neutral congestion is charged exactly the base price, so that point
    always belongs in the range.
    """
    factor = current_base / base
    low, high = round(low * factor), round(high * factor)
    low, high = min(low, current_base), max(high, current_base)
    return max(settings.FEED_PRICE_MIN, low), min(settings.FEED_PRICE_MAX, high)


async def read_price_range(
    redis: Redis, channel_id: int | None = None
) -> tuple[int, int] | None:
    """The observed price range, or None if nothing has ever been observed.

    `channel_id` scopes it to one channel's routes; omitted, it covers every route
    anyone has priced.

    Exact within the current window — every route price computed in it widens this. A
    spread left over from an *earlier* window is not reported as-is, since it was
    measured against a base price that has moved, but rescaled onto the current base by
    `_rescale_range`. That is a much better answer than the base price at both ends,
    which claims every route costs the same: the composer's range would collapse to a
    single number about once per window — whenever a client refreshed before that
    window's first channel-list load — and then quote one *lower* than what
    `create_post` would actually charge.

    None is still a real answer every caller has to handle: a deployment where nobody
    has priced a route since the key was last evicted has observed nothing at all, and
    there the base price at both ends is the only thing left to say.
    """
    key = (
        keys.PRICE_RANGE
        if channel_id is None
        else keys.channel_price_range(channel_id)
    )
    window, low, high, base = await redis.hmget(key, ["window", "min", "max", "base"])
    if window is None or low is None or high is None:
        return None
    snapshot = await get_price_snapshot(redis)
    if window == str(int(snapshot["expires_at"])):
        return int(low), int(high)
    # An earlier window's spread. `base` is absent from one written before it was
    # recorded, and without it there is nothing to say what those numbers were relative
    # to — so that single window degrades to the old behaviour rather than guessing.
    if base is None or int(base) <= 0:
        return None
    return _rescale_range(int(low), int(high), int(base), snapshot["price"])


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
            log.exception("feed.price_refresh_failed")
        try:
            await asyncio.sleep(settings.FEED_PRICE_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise


async def schedule_retry(
    redis: Redis,
    post_id: int,
    channel_id: int,
    language: str,
    delay: float | None = None,
    expires_at: float | None = None,
    author_id: str | None = None,
    fanout: int | None = None,
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

    `author_id`, `language` and `fanout` ride along the same way, so a parked op still
    knows where to route, whose author to skip, and how many recipients it is worth when
    it is eventually re-added to the stream.
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
            "language": language,
            "expires_at": expires_at,
            "author_id": author_id,
            "fanout": fanout,
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
        # Parked before language routing existed. UNSPECIFIED is the honest reading of
        # an op that never chose: it routes through the whole channel, which is exactly
        # the audience the op was minted against, so a backlog crossing the deploy is
        # delivered as originally intended rather than narrowed to one language slice.
        language = op.get("language") or UNSPECIFIED
        expires_at = op.get("expires_at")
        if expires_at is None:
            # Parked before deadlines existed. Stamp one from now: the upgrade neither
            # discards a live backlog nor leaves ops that can never expire.
            expires_at = now + settings.FEED_RETRY_MAX_AGE_SECONDS
        elif expires_at <= now:
            await redis.zrem(keys.OPS_RETRY, member)
            # Terminal: this op will never be delivered, so it stops counting against
            # the route's price.
            await retire_operation(redis, op["channel_id"], language)
            log.info(
                "feed.op_abandoned",
                post_id=op["post_id"],
                channel_id=op["channel_id"],
                language=language,
                reason="retry_deadline",
                max_age_seconds=settings.FEED_RETRY_MAX_AGE_SECONDS,
            )
            continue
        # XADD before ZREM: if the process dies in between, the op is merely duplicated
        # (already-tolerated, see claim_from_queue/review_post's IntegrityError
        # handling) rather than silently lost.
        #
        # `_add_to_stream`, not `enqueue_operation`: this op is already counted against
        # its route and has been throughout its time parked here.
        await _add_to_stream(
            redis,
            op["post_id"],
            op["channel_id"],
            language,
            expires_at,
            op.get("author_id"),
            # Absent on ops parked before Reviewer Trust existed, which then fan out at
            # FEED_FANOUT — the reach they were minted under.
            op.get("fanout"),
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


# --- erasure ---------------------------------------------------------------

async def mark_posts_deleted(redis: Redis, post_ids: Sequence[int]) -> None:
    """Record that these posts no longer exist, and forget who had them.

    Two effects, both about work that is already in flight. The tombstones
    (`keys.deleted_post`) are what stop the worker manufacturing new ghost
    entries out of operations minted before the deletion - it never reads
    Postgres, so this is the only place it can learn. Dropping the `seen:*` sets
    is just housekeeping: the guard they implement is meaningless for a post
    nobody can be handed any more, and they are the largest keys involved.

    Nothing here touches the queues those ids already sit in. That is deliberate
    (and the whole reason the ghost card exists): finding them would mean
    scanning every reader's queue, and the feed answers the question correctly
    the next time it is asked anyway.
    """
    if not post_ids:
        return
    pipe = redis.pipeline()
    for post_id in post_ids:
        pipe.set(
            keys.deleted_post(post_id), 1, ex=settings.FEED_RETRY_MAX_AGE_SECONDS
        )
        pipe.delete(keys.seen(post_id))
    await pipe.execute()


async def is_post_deleted(redis: Redis, post_id: int) -> bool:
    """Whether `post_id` has been erased (see `mark_posts_deleted`).

    False for a post erased longer ago than FEED_RETRY_MAX_AGE_SECONDS, which is
    fine: no operation can still be chasing it by then.
    """
    return bool(await redis.exists(keys.deleted_post(post_id)))


async def purge_user(
    redis: Redis, user_id: str, channel_ids: Sequence[int] = ()
) -> None:
    """Erase every trace of a user from the distribution state.

    Called after the account's rows are committed away (see
    app/core/account_deletion.py). `channel_ids` are the channels they were
    subscribed to, read from Postgres *before* the delete - the subscription
    rows are gone by the time this runs, and `subs:total` is a running counter
    that would drift permanently if a departure went unrecorded.

    The rate-limit and email-verification keys are left to expire on their own:
    both are short-lived by construction, and neither means anything once no
    token can authenticate as this id again.
    """
    pipe = redis.pipeline()
    pipe.delete(keys.queue(user_id))
    pipe.delete(keys.tokens(user_id))
    pipe.srem(keys.FREE_QUEUE, user_id)
    pipe.zrem(keys.ACTIVE_USERS, user_id)
    # Every audience set of every channel they were in, not just the ones their
    # `content_languages` named: the account's rows are already committed away by the
    # time this runs, so there is nothing left to read a current language set from,
    # and a missed membership would keep fanning posts at a user id that no longer
    # exists. SREM of a non-member is free, and the loop is bounded by
    # CONTENT_LANGUAGES.
    for channel_id in channel_ids:
        pipe.srem(keys.channel(channel_id), user_id)
        for language in languages_module.reading_languages():
            pipe.srem(keys.audience(channel_id, language), user_id)
    results = await pipe.execute()
    # Only the memberships that were actually there may decrement the running
    # total - the same rule `sync_unsubscribe` follows, and for the same reason:
    # a re-run must not drive the denominator of the price formula negative.
    removed = sum(int(r) for r in results[4:])
    if removed:
        await redis.decrby(keys.SUBS_TOTAL, removed)


# --- recipient selection ---------------------------------------------------

async def select_recipients(
    redis: Redis,
    channel_id: int,
    language: str,
    k: int,
    post_id: int | None = None,
    author_id: str | None = None,
) -> list[str]:
    """Pick up to `k` distinct random user_ids in this route's audience *and* free.

    Sample-then-filter, not intersect: `SRANDMEMBER` a bounded random sample of the
    route's audience (`k * FEED_FANOUT_SAMPLE_MULTIPLIER`), then keep those in
    `free_queue` via a single `SMISMEMBER`. Both calls are O(sample), independent of
    audience size — unlike a per-op `SINTERSTORE(channel, free_queue)`, which scans a
    set proportional to the whole channel (or the global free set) and blocks the
    single-threaded server for that long on *every* operation.

    **Language is part of the key, not one of the filters** (see keys.audience), and
    that is what keeps this function's cost unchanged by language routing. Filtering
    the sample by language instead would have been a fourth `SMISMEMBER` and looked
    cheaper, but it samples from the wrong population: a language read by a tenth of a
    channel leaves roughly one usable candidate out of the twelve drawn, so nearly
    every op would park and retry while the backlog inflated everyone's price.

    Trade-off: for an audience large enough that the sample is a strict subset, this is
    probabilistic — if free subscribers are rare, the sample may miss them and the op
    is parked for retry (correct: the route is genuinely congested). For audiences at
    or below the sample size the whole set is drawn, so selection is exact, matching
    the old behaviour. Oversampling (the multiplier) keeps the miss rate low until a
    route is heavily saturated.

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
    candidates = await redis.srandmember(
        keys.audience(channel_id, language), sample_size
    )
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
    redis: Redis,
    channel_id: int,
    language: str,
    post_id: int,
    author_id: str | None = None,
) -> bool:
    """Could *any* member of this route's audience still receive this post, ignoring
    queue capacity?

    Separates "undeliverable right now" from "undeliverable forever". Before exclusions
    existed every subscriber was always a valid target, so an empty recipient list only
    ever meant full queues, and parking for retry was always the right answer. Exclusions
    make a post able to genuinely run out of audience — and parking one of those would
    cycle it through the stream every FEED_RETRY_INTERVAL_SECONDS until
    FEED_RETRY_MAX_AGE_SECONDS: ~43k pointless fan-out attempts over 10 days per
    saturated post, each one inflating XLEN and therefore the admission price everyone
    pays. That arithmetic is why this gate got *more* valuable when the deadline was
    doubled, not less.

    **It measures the route's audience, never the channel's**, and that distinction is
    load-bearing rather than tidy. Asking the channel set would let a German post whose
    twenty German readers have all seen it observe five thousand English subscribers,
    conclude it still had an audience, and park itself for ten days — inflating the
    price of every post in the channel to chase readers it can never be delivered to.
    Language routing is also what makes exhaustion *ordinary*: a small route runs out
    after a handful of forwards, so this path went from a rare edge case to the normal
    end of a post's life.

    Cheap gate first. Exhaustion requires the seen set to cover every member bar at
    most the author, so `seen + 1 < audience` rules it out with two O(1) SCARDs — the
    common "everyone is merely full" case never pays more than that. Only when the gate
    passes do we spend the O(audience) SDIFF, and a confirmed exhaustion abandons the
    op, so that scan does not recur for the same post.

    Note the seen set is per *post*, not per route, so a post that has been read in one
    route counts those readers here too. That is correct rather than incidental: a
    reader is excluded because they have already had this post, whichever audience set
    delivered it — which is what keeps an UNSPECIFIED post from being handed to someone
    twice through two different routes.

    An audience with *no* members is deliberately treated as still-eligible. Subscribing
    pulls no history, so a parked op is the only way a brand-new channel's backlog ever
    reaches its first subscriber (see tests/feed/test_worker.py::TestBacklogDelivery),
    and the same now goes for the first reader to opt into a language.
    Exhaustion is the narrower claim: an audience exists, and every one of them is
    already excluded.
    """
    if not (settings.FEED_EXCLUDE_SEEN or settings.FEED_EXCLUDE_OWN_POSTS):
        return True

    audience_key = keys.audience(channel_id, language)
    audience_size = await redis.scard(audience_key)
    if audience_size == 0:
        return True

    seen_count = (
        await redis.scard(keys.seen(post_id)) if settings.FEED_EXCLUDE_SEEN else 0
    )
    # +1 covers the author, who may be in the audience but is never in the seen set
    # (they are excluded before delivery, so they never get placed).
    if seen_count + 1 < audience_size:
        return True

    if settings.FEED_EXCLUDE_SEEN:
        remaining = await redis.sdiff([audience_key, keys.seen(post_id)])
    else:
        remaining = await redis.smembers(audience_key)
    if author_id is not None and settings.FEED_EXCLUDE_OWN_POSTS:
        remaining.discard(author_id)
    return bool(remaining)


# --- subscription sync -----------------------------------------------------

async def sync_subscribe(
    redis: Redis, user_id: str, channel_id: int, languages: Sequence[str]
) -> None:
    """Reflect a subscription: join every audience set this reader belongs in for the
    channel, and ensure they are reachable via free_queue.

    One subscription is several memberships — the plain channel set (which is both the
    UNSPECIFIED audience and the union used for counting) plus one set per language the
    reader accepts. That fan-out of writes is the entire cost of language routing, and
    it is paid here, on a rare action, rather than on every delivery.

    The running membership total (keys.SUBS_TOTAL) moves by however many SADDs actually
    added — subscribing twice must not inflate it, and the endpoint is deliberately
    idempotent.
    """
    pipe = redis.pipeline()
    pipe.sadd(keys.channel(channel_id), user_id)
    for language in languages:
        pipe.sadd(keys.audience(channel_id, language), user_id)
    added = sum(int(r) for r in await pipe.execute())
    if added:
        await redis.incrby(keys.SUBS_TOTAL, added)
    await get_scripts(redis).ensure_free(
        keys=[keys.queue(user_id), keys.FREE_QUEUE],
        args=[user_id, settings.FEED_QUEUE_MAX_SLOTS],
    )


async def sync_unsubscribe(redis: Redis, user_id: str, channel_id: int) -> None:
    """Reflect an unsubscription: remove the user from every one of the channel's
    audience sets.

    Clears *all* configured languages rather than the ones the reader currently
    accepts, deliberately. Their accepted set may have changed since they subscribed,
    or a rebuild may have written memberships from a different set, and a leftover
    membership is not benign: it keeps delivering posts from a channel the reader has
    left. `SREM` of a non-member is free, and the loop is bounded by CONTENT_LANGUAGES,
    so over-clearing costs a few no-ops and closes the whole class of drift.

    Only memberships that were really there may decrement the total, for the same
    reason `purge_user` follows that rule: a re-run must not drive the denominator of
    the price formula negative.
    """
    pipe = redis.pipeline()
    pipe.srem(keys.channel(channel_id), user_id)
    for language in languages_module.reading_languages():
        pipe.srem(keys.audience(channel_id, language), user_id)
    removed = sum(int(r) for r in await pipe.execute())
    if removed:
        await redis.decrby(keys.SUBS_TOTAL, removed)


async def sync_content_languages(
    redis: Redis,
    user_id: str,
    channel_ids: Sequence[int],
    languages: Sequence[str],
) -> None:
    """Rewrite a reader's language memberships across every channel they subscribe to.

    Called when `User.content_languages` changes. Cost is `channels x languages` SADD/
    SREM in one pipeline — bounded, rare, and the reason the delivery path pays nothing:
    the expense of language routing is concentrated in this one infrequent write rather
    than spread across every fan-out.

    Sets the accepted languages and clears the rest in the same pass, rather than
    diffing against the previous value. The previous value is what the *database* said,
    not necessarily what Redis holds (a failed sync, a partial rebuild, a deploy that
    changed CONTENT_LANGUAGES), and a diff propagates that drift forever while an
    absolute rewrite ends it. The plain channel set is untouched — it is membership in
    the channel, not in a language.
    """
    accepted = set(languages)
    plan = [
        (keys.audience(channel_id, language), language in accepted)
        for channel_id in channel_ids
        for language in languages_module.reading_languages()
    ]
    if not plan:
        return

    pipe = redis.pipeline()
    for key, keep in plan:
        pipe.sadd(key, user_id) if keep else pipe.srem(key, user_id)
    results = await pipe.execute()

    # SADD and SREM both answer "how many members actually changed", so one signed sum
    # over the plan gives the net membership delta: an add that found the user already
    # there returns 0 and moves nothing, which is what makes re-running this a no-op.
    delta = sum(
        int(changed) if keep else -int(changed)
        for (_key, keep), changed in zip(plan, results, strict=True)
    )
    if delta:
        await redis.incrby(keys.SUBS_TOTAL, delta)


async def backfill_queue(
    redis: Redis,
    session: AsyncSession,
    user_id: uuid.UUID,
    channel_id: int,
    languages: Sequence[str],
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
        # The live path never has to filter by language - it samples from an audience
        # set that is already the right population - but this one queries Postgres
        # directly and has no such set to lean on. Without it a rebuild would hand a
        # reader every post in the channel regardless of language, undoing the routing
        # for exactly the users a rebuild is meant to restore. UNSPECIFIED is always
        # included: those posts route through the whole channel, so every subscriber
        # is in their audience.
        Post.language.in_([*languages, UNSPECIFIED]),
        # Test posts are minted for one named reader and placed straight into their
        # queue (see app/core/probes.py); they are never anyone else's to receive.
        # Without this a rebuild would hand strangers a probe that was measuring
        # somebody else, and score them on it.
        Post.is_probe.is_(False),
    ]
    if settings.FEED_EXCLUDE_OWN_POSTS:
        # The fan-out path skips the author via the stream entry, which this path has
        # no equivalent of — filter in SQL instead, or a rebuild would hand authors
        # back their own posts that live delivery had correctly withheld.
        #
        # IS DISTINCT FROM, not `!=`: a post whose author deleted their account
        # carries a NULL author_id (see app/core/account_deletion.py), and
        # `NULL != <uuid>` is NULL, so a plain inequality would quietly drop every
        # authorless post from every rebuild instead of keeping it in circulation.
        filters.append(Post.author_id.is_distinct_from(user_id))
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
    """Recount the per-route outstanding-op counters from the ops actually in flight
    (the stream plus `ops:retry`). Returns the total counted.

    Also the migration path onto route keying: the hash is deleted and rewritten, so
    entries left behind under bare channel ids by a pre-routing deploy are cleared
    rather than lingering as backlog no route can ever retire.

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
            # Entries predating language routing carry no language and are counted
            # under UNSPECIFIED - the same route they will actually be fanned out on.
            route = keys.route(channel_id, fields.get("language") or UNSPECIFIED)
            counts[route] = counts.get(route, 0) + 1
    for member in await redis.zrange(keys.OPS_RETRY, 0, -1):
        _, _, payload = member.partition(":")
        op = json.loads(payload)
        route = keys.route(op["channel_id"], op.get("language") or UNSPECIFIED)
        counts[route] = counts.get(route, 0) + 1

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
      a rebuild). Tokens earned by answering test posts are not in that proxy either,
      since a probe deliberately moves no counter; next to the spends this already
      discards, that is a rounding error and not worth a second query.
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
    languages_by_user = {user.id: user.content_languages for user in users}

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
    memberships = 0
    for sub in subs:
        languages = languages_by_user.get(sub.user_id) or []
        await redis.sadd(keys.channel(sub.channel_id), str(sub.user_id))
        for language in languages:
            await redis.sadd(
                keys.audience(sub.channel_id, language), str(sub.user_id)
            )
        # The plain channel set plus one per accepted language - the same arithmetic
        # `sync_subscribe` does incrementally, which is what SUBS_TOTAL counts.
        memberships += 1 + len(languages)
        backfilled += await backfill_queue(
            redis, session, sub.user_id, sub.channel_id, languages
        )

    # Set from Postgres rather than summing what SADD just added: a rebuild over sets
    # that already held most members would otherwise count only the new ones.
    await redis.set(keys.SUBS_TOTAL, memberships)
    outstanding = await reseed_outstanding_ops(redis)

    return {
        "subscriptions": len(subs),
        "users": len(users),
        "backfilled": backfilled,
        "seen_seeded": seen_seeded,
        "outstanding_ops": outstanding,
    }
