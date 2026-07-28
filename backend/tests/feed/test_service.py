import json
import time
import uuid

from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service
from tests.utils import subscribe


class TestFreeQueueInvariant:
    async def test_place_removes_when_full_claim_re_adds(self, redis: Redis):
        uid = str(uuid.uuid4())
        await service.sync_subscribe(redis, uid, 1)
        assert await redis.sismember(keys.FREE_QUEUE, uid)

        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, uid, post_id)
        # Full ⇒ dropped from free_queue.
        assert not await redis.sismember(keys.FREE_QUEUE, uid)

        removed = await service.claim_from_queue(redis, uid, 1)
        assert removed == 1
        # A slot freed ⇒ back in free_queue.
        assert await redis.sismember(keys.FREE_QUEUE, uid)

    async def test_claim_missing_post_returns_zero(self, redis: Redis):
        uid = str(uuid.uuid4())
        assert await service.claim_from_queue(redis, uid, 999) == 0

    async def test_place_is_idempotent_per_post(self, redis: Redis):
        uid = str(uuid.uuid4())
        await service.sync_subscribe(redis, uid, 1)

        assert await service.place_post(redis, uid, 5) == 1
        # Second delivery of the same post is a no-op, not a second copy.
        assert await service.place_post(redis, uid, 5) == 1
        assert await service.render_queue_ids(redis, uid, 10) == [5]
        # One claim empties it — there is no leftover duplicate to review again.
        assert await service.claim_from_queue(redis, uid, 5) == 1
        assert await service.render_queue_ids(redis, uid, 10) == []


class TestTokens:
    async def test_spend_only_when_affordable(self, redis: Redis):
        uid = str(uuid.uuid4())
        await service.earn_token(redis, uid, 3)

        assert await service.spend_tokens(redis, uid, 2) == 1
        # Insufficient: no change, returns None.
        assert await service.spend_tokens(redis, uid, 5) is None
        assert await service.token_balance(redis, uid) == 1


class TestRecipientSelection:
    async def test_selects_only_free_subscribers(self, redis: Redis):
        channel_id = 42
        free_user = str(uuid.uuid4())
        full_user = str(uuid.uuid4())
        await service.sync_subscribe(redis, free_user, channel_id)
        await service.sync_subscribe(redis, full_user, channel_id)
        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, full_user, post_id)

        recipients = await service.select_recipients(redis, channel_id, 10)
        assert free_user in recipients
        assert full_user not in recipients

    async def test_no_subscribers_returns_empty(self, redis: Redis):
        assert await service.select_recipients(redis, 777, 5) == []

    async def test_returns_at_most_k_free(self, redis: Redis):
        channel_id = 43
        for _ in range(10):
            await service.sync_subscribe(redis, str(uuid.uuid4()), channel_id)
        recipients = await service.select_recipients(redis, channel_id, 3)
        assert len(recipients) == 3
        assert len(set(recipients)) == 3

    async def test_all_full_channel_returns_empty(self, redis: Redis):
        channel_id = 44
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, user, post_id)
        # Sole subscriber is full ⇒ not selectable, op would be parked for retry.
        assert await service.select_recipients(redis, channel_id, 3) == []


class TestOperationRetry:
    async def test_reschedule_readds_due_op_to_stream(self, redis: Redis):
        # A stalled op (post 1) plus a newer op (post 2) already on the stream.
        await service.schedule_retry(redis, post_id=1, channel_id=1, delay=-1)
        await service.enqueue_operation(redis, post_id=2, channel_id=1)

        moved = await service.reschedule_due_retries(redis)
        assert moved == 1
        # The due retry is re-added to the stream tail (streams are append-only).
        assert await redis.xlen(keys.STREAM) == 2
        assert await redis.zcard(keys.OPS_RETRY) == 0

    async def test_reschedule_ignores_not_yet_due(self, redis: Redis):
        await service.schedule_retry(redis, post_id=1, channel_id=1, delay=60)
        assert await service.reschedule_due_retries(redis) == 0
        assert await redis.xlen(keys.STREAM) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 1

    async def test_expired_op_is_abandoned_not_rescheduled(self, redis: Redis):
        # Past FEED_RETRY_MAX_AGE_SECONDS ⇒ dropped, so a channel that never gains a
        # free subscriber stops cycling its posts through the stream.
        await service.schedule_retry(
            redis, post_id=1, channel_id=1, delay=-1, expires_at=time.time() - 1
        )
        assert await service.reschedule_due_retries(redis) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 0
        assert await redis.xlen(keys.STREAM) == 0

    async def test_reschedule_carries_deadline_onto_the_stream(self, redis: Redis):
        # The deadline rides along with the op so the next park can reuse it.
        deadline = time.time() + 3600
        await service.schedule_retry(
            redis, post_id=1, channel_id=1, delay=-1, expires_at=deadline
        )
        assert await service.reschedule_due_retries(redis) == 1

        entries = await redis.xrange(keys.STREAM)
        assert float(entries[0][1]["expires_at"]) == deadline

    async def test_legacy_op_without_deadline_is_kept_and_stamped(self, redis: Redis):
        # Ops parked before expiry existed have no `expires_at`. An upgrade must not
        # discard them, but must give them a deadline so they cannot retry forever.
        payload = json.dumps({"post_id": 1, "channel_id": 1})
        await redis.zadd(keys.OPS_RETRY, {f"legacy:{payload}": time.time() - 1})

        assert await service.reschedule_due_retries(redis) == 1
        entries = await redis.xrange(keys.STREAM)
        assert len(entries) == 1
        assert float(entries[0][1]["expires_at"]) > time.time()

    async def test_schedule_retry_disambiguates_same_post_and_channel(
        self, redis: Redis
    ):
        # Two stalled attempts for the same post/channel (e.g. create + forward)
        # must not collide into a single sorted-set entry.
        await service.schedule_retry(redis, post_id=1, channel_id=1, delay=-1)
        await service.schedule_retry(redis, post_id=1, channel_id=1, delay=-1)
        assert await redis.zcard(keys.OPS_RETRY) == 2

        moved = await service.reschedule_due_retries(redis)
        assert moved == 2
        assert await redis.xlen(keys.STREAM) == 2


class TestPostgresBridge:
    async def test_backfill_seeds_recent_unreviewed_posts(
        self, redis: Redis, db, create_user, create_channel, create_post
    ):
        user = await create_user()
        channel = await create_channel()
        post_a = await create_post(channel=channel)
        post_b = await create_post(channel=channel)

        count = await service.backfill_queue(redis, db, user.id, channel.id)
        assert count == 2
        ids = await service.render_queue_ids(redis, str(user.id), 10)
        assert set(ids) == {post_a.id, post_b.id}

    async def test_rebuild_populates_channel_set_and_free_queue(
        self, redis: Redis, db, create_user, create_channel
    ):
        user = await create_user()
        channel = await create_channel()
        await subscribe(db, user, channel)

        await service.rebuild_from_pg(redis, db)
        assert await redis.sismember(keys.channel(channel.id), str(user.id))
        assert await redis.sismember(keys.FREE_QUEUE, str(user.id))

    async def test_rebuild_seeds_tokens_with_starting_grant_plus_reviewed_count(
        self, redis: Redis, db, create_user
    ):
        """A rebuild must not retroactively strip a never-reviewed account's
        starting grant — see FEED_STARTING_TOKENS."""
        user = await create_user()
        user.reviewed_count = 3
        db.add(user)
        await db.commit()

        await service.rebuild_from_pg(redis, db)
        assert await service.token_balance(redis, str(user.id)) == (
            settings.FEED_STARTING_TOKENS + 3
        )
