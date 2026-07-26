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
recipients), which the feed already tolerates — duplicate delivery is deduped at
review time by the unique `(user, post)` constraint.

Self-trimming: a fully processed entry is `XDEL`'d and `XACK`'d, so the stream retains
only outstanding work (undelivered backlog + in-flight). No separate trim janitor is
needed; `operation_queue_len` (XLEN) is therefore a fair congestion signal for pricing.

Fan-out itself is pure Redis: the entry carries the channel_id, recipients are a random
sample of the channel's subscribers filtered to those with a free slot (see
service.select_recipients) — no Postgres access needed.
"""

import asyncio
import logging

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core.config import settings
from app.feed import keys, service

logger = logging.getLogger(__name__)


async def process_operation(
    redis: Redis, post_id: int, channel_id: int, expires_at: float | None = None
) -> int:
    """Fan `post_id` out to up to FEED_FANOUT eligible recipients. Returns the count.

    `expires_at` is the retry deadline carried by an op that has already been parked at
    least once; it is passed straight back to `schedule_retry` so re-parking does not
    reset the clock. None means this op has never been parked.
    """
    recipients = await service.select_recipients(
        redis, channel_id, settings.FEED_FANOUT
    )
    for user_id in recipients:
        await service.place_post(redis, user_id, post_id)
    if not recipients:
        # Nobody subscribed to this channel has a free slot: park for retry instead of
        # discarding. Delivered once any subscriber frees a slot or a new one arrives.
        await service.schedule_retry(redis, post_id, channel_id, expires_at=expires_at)
        logger.info(
            "feed op undeliverable, retry scheduled in %ss: post_id=%s channel_id=%s",
            settings.FEED_RETRY_INTERVAL_SECONDS,
            post_id,
            channel_id,
        )
    return len(recipients)


async def _process_and_confirm(redis: Redis, entry_id: str, fields: dict) -> dict:
    """Fan out one stream entry, then retire it (XDEL + XACK).

    XDEL before XACK: if we crash in between, the entry is gone from the stream but
    still pending — a later XAUTOCLAIM finds it, sees it deleted, and self-cleans the
    pending entry (rather than leaving a lingering acked-but-undeleted entry behind).
    """
    post_id = int(fields["post_id"])
    channel_id = int(fields["channel_id"])
    raw_expiry = fields.get("expires_at")
    expires_at = float(raw_expiry) if raw_expiry is not None else None
    await process_operation(redis, post_id, channel_id, expires_at)
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
        logger.info("reclaimed %s abandoned feed op(s)", count)
    return count


async def run_consumer(redis: Redis, consumer: str) -> None:
    """Consume the operation stream as `consumer` until cancelled."""
    logger.info("feed consumer started: %s", consumer)
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
                logger.exception("feed consumer error; continuing")
                await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        logger.info("feed consumer stopped: %s", consumer)
        raise
