"""The operation-queue consumer.

Runs today as an in-process asyncio task started from the app lifespan. The entire
loop lives in :func:`run_consumer` so promoting it to a dedicated worker process is a
~10-line change (a `worker.py` entrypoint that calls `run_consumer`, plus a compose
service). Single consumer ⇒ operations are processed serially ⇒ no SINTER/place races.

Fan-out is pure Redis: the operation carries the channel_id, recipients are the
channel's subscribers intersected with the free-slot set — no Postgres access needed.
"""

import asyncio
import json
import logging

from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service

logger = logging.getLogger(__name__)


async def process_operation(redis: Redis, post_id: int, channel_id: int) -> int:
    """Fan `post_id` out to up to FEED_FANOUT eligible recipients. Returns the count."""
    recipients = await service.select_recipients(
        redis, channel_id, settings.FEED_FANOUT
    )
    for user_id in recipients:
        await service.place_post(redis, user_id, post_id)
    if not recipients:
        # Every subscriber's queue is currently full: park for retry instead of
        # discarding. Delivered once any subscriber (existing or new) frees a slot.
        await service.schedule_retry(redis, post_id, channel_id)
        logger.info(
            "feed op undeliverable, retry scheduled in %ss: post_id=%s channel_id=%s",
            settings.FEED_RETRY_INTERVAL_SECONDS,
            post_id,
            channel_id,
        )
    return len(recipients)


async def consume_once(redis: Redis, timeout: float = 1.0) -> dict | None:
    """Block for one operation (up to `timeout`s) and process it.

    Returns the operation dict, or None if the queue was empty for the whole timeout.
    """
    result = await redis.blpop(keys.OPS, timeout=timeout)
    if result is None:
        return None
    _, raw = result
    op = json.loads(raw)
    await process_operation(redis, op["post_id"], op["channel_id"])
    return op


async def run_consumer(redis: Redis) -> None:
    """Consume the operation queue until cancelled."""
    logger.info("feed consumer started")
    try:
        while True:
            try:
                await service.reschedule_due_retries(redis)
                await consume_once(redis)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("feed consumer error; continuing")
                await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        logger.info("feed consumer stopped")
        raise
