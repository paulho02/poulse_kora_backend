import uuid

from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service
from app.feed.worker import consume_once, process_operation


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

        op = await consume_once(redis, timeout=2.0)
        assert op == {"post_id": 55, "channel_id": channel_id}
        assert 55 in await service.render_queue_ids(redis, user, 10)

    async def test_timeout_returns_none(self, redis: Redis):
        assert await consume_once(redis, timeout=0.1) is None
