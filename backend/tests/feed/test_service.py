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
        await service.sync_subscribe(redis, uid, 1, ["en"])
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
        await service.sync_subscribe(redis, uid, 1, ["en"])

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


class TestPresence:
    async def test_marked_user_counts_as_active(self, redis: Redis):
        uid = str(uuid.uuid4())
        await service.mark_active(redis, uid)
        assert await service.active_user_count(redis) == 1

    async def test_stale_mark_falls_outside_the_window(self, redis: Redis):
        uid = str(uuid.uuid4())
        stale = time.time() - settings.ACTIVE_USER_WINDOW_SECONDS - 1
        await service.mark_active(redis, uid, now=stale)
        assert await service.active_user_count(redis) == 0
        # Also swept from the sorted set itself, not just excluded by the count.
        assert await redis.zcard(keys.ACTIVE_USERS) == 0

    async def test_remark_resets_the_window(self, redis: Redis):
        uid = str(uuid.uuid4())
        stale = time.time() - settings.ACTIVE_USER_WINDOW_SECONDS - 1
        await service.mark_active(redis, uid, now=stale)
        await service.mark_active(redis, uid)
        assert await service.active_user_count(redis) == 1


async def _outstanding(redis: Redis, channel_id: int, language: str = "en") -> int:
    """One route's outstanding-op count. Ops are counted per (channel, language) now,
    so reading one is a two-part key - wrapped here to keep the assertions legible."""
    route = (channel_id, language)
    return (await service.route_outstanding_ops(redis, [route]))[route]


class TestOutstandingOps:
    async def test_minting_counts_and_retiring_uncounts(self, redis: Redis):
        await service.enqueue_operation(redis, post_id=1, channel_id=41, language="en")
        await service.enqueue_operation(redis, post_id=2, channel_id=41, language="en")
        assert await _outstanding(redis, 41) == 2

        await service.retire_operation(redis, 41, "en")
        assert await _outstanding(redis, 41) == 1

    async def test_unknown_channel_reads_zero(self, redis: Redis):
        assert await _outstanding(redis, 4242) == 0

    async def test_retire_floors_at_zero(self, redis: Redis):
        """The feed tolerates an op being processed twice (a reclaimed entry, a
        duplicated retry), so double-retires happen. Left unguarded the count would go
        negative and the channel would sit at the price floor until that many fresh
        posts had paid the debt off."""
        await service.enqueue_operation(redis, post_id=1, channel_id=42, language="en")
        for _ in range(5):
            await service.retire_operation(redis, 42, "en")
        assert await _outstanding(redis, 42) == 0

        await service.enqueue_operation(redis, post_id=2, channel_id=42, language="en")
        assert await _outstanding(redis, 42) == 1

    async def test_total_sums_every_channel(self, redis: Redis):
        await service.enqueue_operation(redis, post_id=1, channel_id=43, language="en")
        await service.enqueue_operation(redis, post_id=2, channel_id=43, language="en")
        await service.enqueue_operation(redis, post_id=3, channel_id=44, language="en")
        assert await service.outstanding_ops_total(redis) == 3

    async def test_retry_readd_does_not_count_again(self, redis: Redis):
        """A parked op was counted when it was minted and stays counted the whole time
        it is parked — re-adding it to the stream is not new work."""
        await service.enqueue_operation(redis, post_id=1, channel_id=45, language="en")
        await service.schedule_retry(
            redis, post_id=1, channel_id=45, language="en", delay=-1
        )

        assert await service.reschedule_due_retries(redis) == 1
        assert await _outstanding(redis, 45) == 1

    async def test_expired_retry_retires_the_op(self, redis: Redis):
        await service.enqueue_operation(redis, post_id=1, channel_id=46, language="en")
        await service.schedule_retry(
            redis,
            post_id=1,
            channel_id=46,
            language="en",
            delay=-1,
            expires_at=time.time() - 1,
        )

        assert await service.reschedule_due_retries(redis) == 0
        assert await _outstanding(redis, 46) == 0

    async def test_reseed_recounts_from_the_stream_and_retry_set(self, redis: Redis):
        await service.enqueue_operation(redis, post_id=1, channel_id=47, language="en")
        await service.enqueue_operation(redis, post_id=2, channel_id=47, language="en")
        await service.schedule_retry(redis, post_id=3, channel_id=48, language="en")
        # Drift the counters away from the truth, as a crash or a flush would.
        await redis.hset(keys.OPS_OUTSTANDING, "47", 99)
        await redis.hset(keys.OPS_OUTSTANDING, "999", 5)

        total = await service.reseed_outstanding_ops(redis)

        counts = await service.route_outstanding_ops(
            redis, [(47, "en"), (48, "en"), (999, "en")]
        )
        assert counts == {(47, "en"): 2, (48, "en"): 1, (999, "en"): 0}
        assert total == 3


class TestSubscriptionTotal:
    async def test_tracks_subscribe_and_unsubscribe(self, redis: Redis):
        """The counter is *memberships*, not subscriptions: one subscription puts a
        reader in the plain channel set plus one set per language they accept, and
        `route_factor` divides a single route's audience by this total."""
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        await service.sync_subscribe(redis, a, 51, ["en"])
        await service.sync_subscribe(redis, b, 51, ["en"])
        assert await service.subscription_total(redis) == 4

        await service.sync_unsubscribe(redis, a, 51)
        assert await service.subscription_total(redis) == 2

    async def test_counts_one_membership_per_accepted_language(self, redis: Redis):
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, 53, ["en", "de"])
        # The channel set, plus one per language.
        assert await service.subscription_total(redis) == 3

        # Unsubscribing clears every language, not just the ones passed in when
        # subscribing - which is what keeps a changed preference from stranding a
        # membership that would go on delivering.
        await service.sync_unsubscribe(redis, user, 53)
        assert await service.subscription_total(redis) == 0

    async def test_repeat_calls_do_not_double_count(self, redis: Redis):
        """Both endpoints are idempotent, so the counter must move with the sets, not
        with the number of requests."""
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, 52, ["en"])
        await service.sync_subscribe(redis, user, 52, ["en"])
        assert await service.subscription_total(redis) == 2

        await service.sync_unsubscribe(redis, user, 52)
        await service.sync_unsubscribe(redis, user, 52)
        assert await service.subscription_total(redis) == 0


class TestRecipientSelection:
    async def test_selects_only_free_subscribers(self, redis: Redis):
        channel_id = 42
        free_user = str(uuid.uuid4())
        full_user = str(uuid.uuid4())
        await service.sync_subscribe(redis, free_user, channel_id, ["en"])
        await service.sync_subscribe(redis, full_user, channel_id, ["en"])
        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, full_user, post_id)

        recipients = await service.select_recipients(redis, channel_id, "en", 10)
        assert free_user in recipients
        assert full_user not in recipients

    async def test_no_subscribers_returns_empty(self, redis: Redis):
        assert await service.select_recipients(redis, 777, "en", 5) == []

    async def test_returns_at_most_k_free(self, redis: Redis):
        channel_id = 43
        for _ in range(10):
            await service.sync_subscribe(redis, str(uuid.uuid4()), channel_id, ["en"])
        recipients = await service.select_recipients(redis, channel_id, "en", 3)
        assert len(recipients) == 3
        assert len(set(recipients)) == 3

    async def test_all_full_channel_returns_empty(self, redis: Redis):
        channel_id = 44
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id, ["en"])
        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, user, post_id)
        # Sole subscriber is full ⇒ not selectable, op would be parked for retry.
        assert await service.select_recipients(redis, channel_id, "en", 3) == []


class TestOperationRetry:
    async def test_reschedule_readds_due_op_to_stream(self, redis: Redis):
        # A stalled op (post 1) plus a newer op (post 2) already on the stream.
        await service.schedule_retry(
            redis, post_id=1, channel_id=1, language="en", delay=-1
        )
        await service.enqueue_operation(redis, post_id=2, channel_id=1, language="en")

        moved = await service.reschedule_due_retries(redis)
        assert moved == 1
        # The due retry is re-added to the stream tail (streams are append-only).
        assert await redis.xlen(keys.STREAM) == 2
        assert await redis.zcard(keys.OPS_RETRY) == 0

    async def test_reschedule_ignores_not_yet_due(self, redis: Redis):
        await service.schedule_retry(
            redis, post_id=1, channel_id=1, language="en", delay=60
        )
        assert await service.reschedule_due_retries(redis) == 0
        assert await redis.xlen(keys.STREAM) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 1

    async def test_expired_op_is_abandoned_not_rescheduled(self, redis: Redis):
        # Past FEED_RETRY_MAX_AGE_SECONDS ⇒ dropped, so a channel that never gains a
        # free subscriber stops cycling its posts through the stream.
        await service.schedule_retry(
            redis,
            post_id=1,
            channel_id=1,
            language="en",
            delay=-1,
            expires_at=time.time() - 1,
        )
        assert await service.reschedule_due_retries(redis) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 0
        assert await redis.xlen(keys.STREAM) == 0

    async def test_reschedule_carries_deadline_onto_the_stream(self, redis: Redis):
        # The deadline rides along with the op so the next park can reuse it.
        deadline = time.time() + 3600
        await service.schedule_retry(
            redis, post_id=1, channel_id=1, language="en", delay=-1, expires_at=deadline
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
        await service.schedule_retry(
            redis, post_id=1, channel_id=1, language="en", delay=-1
        )
        await service.schedule_retry(
            redis, post_id=1, channel_id=1, language="en", delay=-1
        )
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

        count = await service.backfill_queue(redis, db, user.id, channel.id, ["en"])
        assert count == 2
        ids = await service.render_queue_ids(redis, str(user.id), 10)
        assert set(ids) == {post_a.id, post_b.id}

    async def test_backfill_is_a_noop_when_the_queue_is_already_full(
        self, redis: Redis, db, create_user, create_channel, create_post
    ):
        user = await create_user()
        channel = await create_channel()
        await create_post(channel=channel)
        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, str(user.id), post_id)

        count = await service.backfill_queue(redis, db, user.id, channel.id, ["en"])
        assert count == 0

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
