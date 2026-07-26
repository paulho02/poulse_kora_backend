import json
import time
import uuid

import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service
from app.feed.worker import (
    consume_once,
    process_operation,
    reclaim_orphaned_operations,
)

CONSUMER = "test-consumer"


async def _retry_deadline(redis: Redis) -> float:
    """`expires_at` of the single op currently parked in ops:retry."""
    (member,) = await redis.zrange(keys.OPS_RETRY, 0, -1)
    _, _, payload = member.partition(":")
    return json.loads(payload)["expires_at"]


class TestProcessOperation:
    async def test_fans_out_to_at_most_k_eligible(self, redis: Redis):
        channel_id = 7
        users = [str(uuid.uuid4()) for _ in range(5)]
        for u in users:
            await service.sync_subscribe(redis, u, channel_id)

        placed = await process_operation(redis, post_id=100, channel_id=channel_id)
        assert placed == settings.FEED_FANOUT

        recipients = [
            u for u in users if 100 in await service.render_queue_ids(redis, u, 10)
        ]
        assert len(recipients) == settings.FEED_FANOUT

    async def test_no_eligible_recipients_schedules_retry(self, redis: Redis):
        # No subscribers to this channel ⇒ undeliverable, parked for retry, no error.
        assert await process_operation(redis, post_id=1, channel_id=999) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 1


class TestConsumeOnce:
    async def test_processes_enqueued_operation(self, redis: Redis):
        channel_id = 8
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        await service.enqueue_operation(redis, post_id=55, channel_id=channel_id)

        op = await consume_once(redis, CONSUMER, timeout=2.0)
        assert op == {"post_id": 55, "channel_id": channel_id}
        assert 55 in await service.render_queue_ids(redis, user, 10)

    async def test_timeout_returns_none(self, redis: Redis):
        # Self-heals the missing group, then blocks briefly on the empty stream.
        assert await consume_once(redis, CONSUMER, timeout=0.1) is None

    async def test_success_retires_entry(self, redis: Redis):
        await service.sync_subscribe(redis, str(uuid.uuid4()), 8)
        await service.enqueue_operation(redis, post_id=55, channel_id=8)

        await consume_once(redis, CONSUMER, timeout=2.0)
        # Processed entry is XDEL'd + XACK'd ⇒ stream self-trims, none left pending.
        assert await redis.xlen(keys.STREAM) == 0
        summary = await redis.xpending(keys.STREAM, keys.STREAM_GROUP)
        assert summary["pending"] == 0


class TestReliableQueue:
    async def test_crashed_op_stays_pending_then_reclaimed(
        self, redis: Redis, monkeypatch
    ):
        # A crash mid-fan-out must leave the op pending (not lost), then be reclaimed
        # and completed by another consumer.
        channel_id = 3
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        await service.enqueue_operation(redis, post_id=77, channel_id=channel_id)

        async def boom(*args, **kwargs):
            raise RuntimeError("crash mid-fanout")

        monkeypatch.setattr("app.feed.worker.process_operation", boom)
        with pytest.raises(RuntimeError):
            await consume_once(redis, "dead-consumer", timeout=2.0)

        # Delivered but never acked ⇒ still in the stream, pending.
        assert await redis.xlen(keys.STREAM) == 1
        assert (await redis.xpending(keys.STREAM, keys.STREAM_GROUP))["pending"] == 1

        # A live consumer reclaims it (idle threshold dropped to 0 for the test).
        monkeypatch.undo()
        monkeypatch.setattr(settings, "FEED_STREAM_CLAIM_MIN_IDLE_MS", 0)
        assert await reclaim_orphaned_operations(redis, "live-consumer") == 1
        assert 77 in await service.render_queue_ids(redis, user, 10)
        # Completed ⇒ retired from the stream and the pending list.
        assert await redis.xlen(keys.STREAM) == 0
        assert (await redis.xpending(keys.STREAM, keys.STREAM_GROUP))["pending"] == 0

    async def test_reclaim_noop_when_nothing_pending(self, redis: Redis):
        await service.ensure_group(redis)
        assert await reclaim_orphaned_operations(redis, CONSUMER) == 0


class TestBacklogDelivery:
    async def test_parked_op_delivers_once_after_subscribe(self, redis: Redis):
        # The reported bug. A post published to a channel with no free recipient is
        # parked; once a user subscribes it must land in their queue exactly once.
        # Subscribing pulls no history, so the retry is the only delivery path.
        channel_id = 11
        user = str(uuid.uuid4())

        assert await process_operation(redis, post_id=90, channel_id=channel_id) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 1

        await service.sync_subscribe(redis, user, channel_id)

        assert await service.reschedule_due_retries(redis, now=time.time() + 3600) == 1
        assert await consume_once(redis, CONSUMER, timeout=2.0) == {
            "post_id": 90,
            "channel_id": channel_id,
        }

        assert await service.render_queue_ids(redis, user, 10) == [90]
        # Delivered ⇒ retired, not parked again.
        assert await redis.zcard(keys.OPS_RETRY) == 0

    async def test_retry_deadline_survives_reparking(self, redis: Redis):
        # Each failed attempt re-parks the op. The deadline must be carried through the
        # stream round-trip rather than recomputed, or FEED_RETRY_MAX_AGE_SECONDS would
        # renew on every attempt and the op would retry forever.
        await service.schedule_retry(redis, post_id=31, channel_id=404, delay=-1)
        original = await _retry_deadline(redis)

        for _ in range(2):
            assert (
                await service.reschedule_due_retries(redis, now=time.time() + 3600) == 1
            )
            # No subscribers to channel 404 ⇒ fan-out fails and re-parks the op.
            await consume_once(redis, CONSUMER, timeout=2.0)
            assert await redis.zcard(keys.OPS_RETRY) == 1
            assert await _retry_deadline(redis) == original
