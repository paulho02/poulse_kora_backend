"""The operation-stream consumer.

Fan-out jobs live in a Redis **Stream** (`keys.STREAM`) consumed by a **consumer group**
(`keys.STREAM_GROUP`). This replaces the old list + hand-rolled reservation/reclaim and
gives two things at once:

- **Crash safety.** An entry read via `XREADGROUP` is added to the group's Pending
  Entries List until the consumer `XACK`s it. If the process dies mid-fan-out, the
  entry stays pending and is reclaimed by `XAUTOCLAIM` (see
  `reclaim_orphaned_operations`) once idle past `FEED_STREAM_CLAIM_MIN_IDLE_MS`. Nothing
  is lost on a crash.
- **Horizontal scale.** Several worker processes may join the same group under distinct
  consumer names; each entry is delivered to exactly one of them. Reclaim is keyed on
  *idle time*, not on a single-consumer assumption, so an op another worker is actively
  processing (recently delivered) is never stolen. This is safe to run with N consumers.

Delivery is at-least-once (a reclaimed, partially-done op re-delivers to some
recipients), which the feed already tolerates — `place_post` refuses a recipient who
has already had the post, and the unique `(user, post)` review constraint is the final
backstop. One consequence worth knowing: because the second pass can no longer land on
the same recipients, a reclaimed op reaches a *fresh* set of users rather than mostly
re-hitting the original ones, so a crash mid-fan-out spreads a post slightly wider than
intended. Over-delivery on crash, never duplicate delivery.

Self-trimming: a fully processed entry is `XDEL`'d and `XACK`'d, so the stream retains
only outstanding work (undelivered backlog + in-flight). No separate trim janitor is
needed; `operation_queue_len` (XLEN) is therefore a fair congestion signal for pricing.

Fan-out itself is pure Redis: the entry carries the routing key (channel_id +
language), recipients are a random sample of that route's audience filtered to those
with a free slot (see service.select_recipients) — no Postgres access needed. That
property is why the language travels on the entry rather than being read off the post,
and why a post
erased by its author is announced *into Redis* (`service.mark_posts_deleted`) rather
than checked for in Postgres here: it is the only way this loop can know, and it costs
one EXISTS per op.
"""

import asyncio
import uuid

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core.config import settings
from app.core.languages import UNSPECIFIED
from app.core.logger import get_logger, new_request_context, reset_request_context
from app.feed import keys, service

log = get_logger(__name__)


async def process_operation(
    redis: Redis,
    post_id: int,
    channel_id: int,
    language: str,
    expires_at: float | None = None,
    author_id: str | None = None,
) -> int:
    """Fan `post_id` out to up to FEED_FANOUT eligible recipients. Returns the count.

    `language` and `channel_id` together are the routing key: they name the audience
    set this post may be delivered into (see service.select_recipients). Neither is
    looked up here — the worker never opens a Postgres session, so everything needed to
    decide where a post goes has to arrive on the stream entry.

    `expires_at` is the retry deadline carried by an op that has already been parked at
    least once; it is passed straight back to `schedule_retry` so re-parking does not
    reset the clock. None means this op has never been parked.

    `author_id` is the post's author, skipped when FEED_EXCLUDE_OWN_POSTS is on. None on
    ops written before the field existed, which simply fan out to everyone as before.
    """
    if await service.is_post_deleted(redis, post_id):
        # The author erased this post after the op was minted. Placing it would
        # hand a reader an id that resolves to nothing - a ghost card they then
        # have to clear by hand - so the op dies here instead. Terminal, and it
        # stops counting against the channel's price like any other terminal
        # outcome.
        await service.retire_operation(redis, channel_id, language)
        # INFO for the same reason as `route_exhausted` below: it is reach the
        # author paid tokens for being given up on, and it is rare.
        log.info(
            "feed.op_abandoned",
            post_id=post_id,
            channel_id=channel_id,
            language=language,
            reason="post_deleted",
        )
        return 0

    recipients = await service.select_recipients(
        redis,
        channel_id,
        language,
        settings.FEED_FANOUT,
        post_id=post_id,
        author_id=author_id,
    )
    # A placement can still be refused after selection (a concurrent worker got there
    # first), so count what actually landed rather than what we intended to send.
    delivered = 0
    for user_id in recipients:
        if await service.place_post(redis, user_id, post_id) != service.PLACE_REFUSED:
            delivered += 1
    if delivered:
        # Terminal outcome: stop counting this op against the route's price.
        await service.retire_operation(redis, channel_id, language)
        # The happy path, and the highest-frequency event in the system (every
        # post and every forward passes through here), so DEBUG - `post.created`
        # and `post.reviewed` already record at INFO that the work exists. What
        # this adds is *where it went*, which only matters while investigating.
        log.debug(
            "feed.op_delivered",
            post_id=post_id,
            channel_id=channel_id,
            language=language,
            delivered=delivered,
            selected=len(recipients),
        )
        return delivered

    if await service.has_eligible_recipient(
        redis, channel_id, language, post_id, author_id
    ):
        # Nobody has a free slot right now: park for retry rather than discarding.
        # Delivered once any subscriber frees a slot or a new one arrives.
        await service.schedule_retry(
            redis,
            post_id,
            channel_id,
            language,
            expires_at=expires_at,
            author_id=author_id,
        )
        # DEBUG on purpose. A parked op re-parks every
        # FEED_RETRY_INTERVAL_SECONDS (20s) for up to FEED_RETRY_MAX_AGE_SECONDS
        # (10 days), so at INFO a single stuck post in a quiet channel would emit
        # ~43k lines on its own. The terminal outcomes below are the ones worth a
        # standing line; this one is what you turn on (LOG_LEVEL_OVERRIDES,
        # {"app.feed": "DEBUG"}) when a specific post is not arriving.
        log.debug(
            "feed.op_parked",
            post_id=post_id,
            channel_id=channel_id,
            language=language,
            retry_in=settings.FEED_RETRY_INTERVAL_SECONDS,
        )
    else:
        # Everyone in this route's audience has already had this post (or the only one
        # left is its author). Parking it would cycle it through the stream every
        # FEED_RETRY_INTERVAL_SECONDS for up to FEED_RETRY_MAX_AGE_SECONDS, inflating
        # the admission price the whole time, to serve only the chance that someone new
        # subscribes. We give that chance up: a post everyone who could read it has
        # already read is not what a new subscriber needs. Note this is strictly
        # narrower than an *empty* audience, which is still parked — that backlog is
        # worth keeping.
        #
        # Language routing made this the *ordinary* end of a post's life rather than an
        # edge case: a route with twenty readers is exhausted after a handful of
        # forwards, where the undivided channel would have kept finding new ones. Which
        # is why `has_eligible_recipient` must measure the route and not the channel -
        # asking the channel here would park every minority-language post for ten days.
        await service.retire_operation(redis, channel_id, language)
        # Terminal and rare, and it is reach an author paid tokens for that is
        # being given up on - so it stays at INFO however busy the stream gets.
        log.info(
            "feed.op_abandoned",
            post_id=post_id,
            channel_id=channel_id,
            language=language,
            reason="route_exhausted",
        )
    return 0


async def _process_and_confirm(redis: Redis, entry_id: str, fields: dict) -> dict:
    """Fan out one stream entry, then retire it (XDEL + XACK).

    XDEL before XACK: if we crash in between, the entry is gone from the stream but
    still pending — a later XAUTOCLAIM finds it, sees it deleted, and self-cleans the
    pending entry (rather than leaving a lingering acked-but-undeleted entry behind).
    """
    post_id = int(fields["post_id"])
    channel_id = int(fields["channel_id"])
    # The worker's answer to the request id: one unit of work, one correlation
    # id, carried by every line the fan-out produces (including a traceback from
    # inside app/feed/service.py). Without it, concurrent ops in the same process
    # interleave into an unreadable stream.
    context = new_request_context(
        request_id=f"op-{uuid.uuid4().hex[:12]}", post_id=post_id, channel_id=channel_id
    )
    try:
        return await _fan_out_and_retire(redis, entry_id, fields, post_id, channel_id)
    finally:
        reset_request_context(context)


async def _fan_out_and_retire(
    redis: Redis, entry_id: str, fields: dict, post_id: int, channel_id: int
) -> dict:
    """The body of `_process_and_confirm`, split out only so the log context it
    runs inside is opened and closed in one place."""
    raw_expiry = fields.get("expires_at")
    expires_at = float(raw_expiry) if raw_expiry is not None else None
    # Absent on ops enqueued before FEED_EXCLUDE_OWN_POSTS existed — tolerated, like
    # `expires_at`, so a deploy doesn't have to drain the stream first.
    author_id = fields.get("author_id")
    # Likewise absent on ops enqueued before language routing. UNSPECIFIED routes
    # through the whole channel, which is the audience such an op was minted against,
    # so a backlog crossing the deploy is delivered as originally intended rather than
    # narrowed to one language's readers.
    language = fields.get("language") or UNSPECIFIED
    await process_operation(
        redis, post_id, channel_id, language, expires_at, author_id
    )
    await redis.xdel(keys.STREAM, entry_id)
    await redis.xack(keys.STREAM, keys.STREAM_GROUP, entry_id)
    return {"post_id": post_id, "channel_id": channel_id}


async def consume_once(
    redis: Redis, consumer: str, timeout: float | None = None
) -> dict | None:
    """Read and process one new stream entry, blocking up to `timeout` seconds.

    Returns the operation dict, or None if nothing arrived within the timeout. Creates
    the group and retries once if it is missing (fresh deploy, or a flushed group).
    """
    if timeout is None:
        timeout = settings.FEED_STREAM_BLOCK_SECONDS
    resp = None
    for created_group in (False, True):
        try:
            resp = await redis.xreadgroup(
                keys.STREAM_GROUP,
                consumer,
                {keys.STREAM: ">"},
                count=1,
                block=int(timeout * 1000),
            )
            break
        except ResponseError as exc:
            if "NOGROUP" in str(exc) and not created_group:
                await service.ensure_group(redis)
                continue
            raise
    if not resp:
        return None
    _stream, entries = resp[0]
    if not entries:
        return None
    entry_id, fields = entries[0]
    return await _process_and_confirm(redis, entry_id, fields)


async def reclaim_orphaned_operations(redis: Redis, consumer: str) -> int:
    """Reclaim and process ops abandoned by a crashed consumer (XAUTOCLAIM).

    Reassigns to `consumer` any entry that has been pending (delivered but unacked)
    longer than FEED_STREAM_CLAIM_MIN_IDLE_MS, then processes and retires each. Returns
    the number reclaimed. Idle-time keying is what makes this safe with >1 consumer.
    """
    try:
        result = await redis.xautoclaim(
            keys.STREAM,
            keys.STREAM_GROUP,
            consumer,
            settings.FEED_STREAM_CLAIM_MIN_IDLE_MS,
            count=settings.FEED_STREAM_RECLAIM_COUNT,
        )
    except ResponseError as exc:
        if "NOGROUP" in str(exc):
            await service.ensure_group(redis)
            return 0
        raise
    # redis-py returns [next_cursor, claimed_entries, deleted_ids]; deleted entries
    # (XDEL'd before their ack) are auto-removed from the PEL and need no handling.
    claimed = result[1] if len(result) > 1 else []
    count = 0
    for entry_id, fields in claimed:
        await _process_and_confirm(redis, entry_id, fields)
        count += 1
    if count:
        # Rare by construction (it takes a crashed or wedged consumer), and it is
        # the visible symptom of one - INFO, and worth alerting on if it repeats.
        log.info("feed.ops_reclaimed", count=count)
    return count


async def run_consumer(redis: Redis, consumer: str) -> None:
    """Consume the operation stream as `consumer` until cancelled."""
    log.info("feed.consumer_started", consumer=consumer)
    await service.ensure_group(redis)
    try:
        while True:
            try:
                await reclaim_orphaned_operations(redis, consumer)
                await service.reschedule_due_retries(redis)
                await consume_once(redis, consumer)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("feed.consumer_error", consumer=consumer)
                await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        log.info("feed.consumer_stopped", consumer=consumer)
        raise
