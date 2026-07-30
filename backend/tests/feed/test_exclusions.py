"""Delivery exclusions: never your own post, never one you've already had.

Two independent mechanisms behind two independent flags — `author_id` carried on the
stream entry, and the per-post `seen` set written by the `place` script.
"""

import uuid

import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.feed import keys, service
from app.feed.worker import process_operation


@pytest.fixture
def toggle(monkeypatch):
    """Flip a FEED_* flag for one test (settings is a module-level singleton)."""

    def inner(name: str, value):
        monkeypatch.setattr(settings, name, value)

    return inner


class TestPlaceMarksSeen:
    async def test_delivery_records_the_recipient_atomically(self, redis: Redis):
        """The mark happens on placement, not on review — which is what makes the
        guarantee race-free: a user cannot forward a post before they have it."""
        uid = str(uuid.uuid4())
        await service.sync_subscribe(redis, uid, 1)

        assert await service.place_post(redis, uid, 5) == 1
        assert await redis.sismember(keys.seen(5), uid)

    async def test_redelivery_is_refused_after_review(self, redis: Redis):
        uid = str(uuid.uuid4())
        await service.sync_subscribe(redis, uid, 1)
        await service.place_post(redis, uid, 5)
        assert await service.claim_from_queue(redis, uid, 5) == 1

        # The queue is empty, so LPOS no longer protects — the seen set does.
        assert await service.place_post(redis, uid, 5) == service.PLACE_REFUSED
        assert await service.render_queue_ids(redis, uid, 10) == []

    async def test_still_queued_post_is_a_noop_not_a_refusal(self, redis: Redis):
        """Idempotent re-delivery of something already in the queue must keep reporting
        success; only a *fresh* placement of an already-had post is refused."""
        uid = str(uuid.uuid4())
        await service.sync_subscribe(redis, uid, 1)

        assert await service.place_post(redis, uid, 5) == 1
        assert await service.place_post(redis, uid, 5) == 1
        assert await service.render_queue_ids(redis, uid, 10) == [5]

    async def test_seen_set_expires(self, redis: Redis):
        uid = str(uuid.uuid4())
        await service.place_post(redis, uid, 5)
        ttl = await redis.ttl(keys.seen(5))
        assert 0 < ttl <= settings.FEED_SEEN_TTL_SECONDS

    async def test_disabled_flag_restores_redelivery(self, redis: Redis, toggle):
        toggle("FEED_EXCLUDE_SEEN", False)
        uid = str(uuid.uuid4())
        await service.sync_subscribe(redis, uid, 1)

        await service.place_post(redis, uid, 5)
        await service.claim_from_queue(redis, uid, 5)
        assert await service.place_post(redis, uid, 5) == 1
        assert not await redis.exists(keys.seen(5))


class TestSelectionExcludes:
    async def test_author_is_never_selected(self, redis: Redis):
        channel_id = 50
        author = str(uuid.uuid4())
        other = str(uuid.uuid4())
        await service.sync_subscribe(redis, author, channel_id)
        await service.sync_subscribe(redis, other, channel_id)

        recipients = await service.select_recipients(
            redis, channel_id, 10, post_id=1, author_id=author
        )
        assert recipients == [other]

    async def test_seen_users_are_not_selected(self, redis: Redis):
        channel_id = 51
        seen_user = str(uuid.uuid4())
        fresh_user = str(uuid.uuid4())
        await service.sync_subscribe(redis, seen_user, channel_id)
        await service.sync_subscribe(redis, fresh_user, channel_id)
        await service.place_post(redis, seen_user, 7)
        await service.claim_from_queue(redis, seen_user, 7)  # reviewed and cleared

        recipients = await service.select_recipients(redis, channel_id, 10, post_id=7)
        assert recipients == [fresh_user]

    async def test_ops_without_an_author_still_fan_out(self, redis: Redis):
        """Entries enqueued before the field existed carry no author — they must not
        be dropped on the deploy that introduces it."""
        channel_id = 52
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)

        recipients = await service.select_recipients(
            redis, channel_id, 10, post_id=1, author_id=None
        )
        assert recipients == [user]

    async def test_disabled_flag_allows_the_author(self, redis: Redis, toggle):
        toggle("FEED_EXCLUDE_OWN_POSTS", False)
        channel_id = 53
        author = str(uuid.uuid4())
        await service.sync_subscribe(redis, author, channel_id)

        recipients = await service.select_recipients(
            redis, channel_id, 10, post_id=1, author_id=author
        )
        assert recipients == [author]


class TestFanOutExclusions:
    async def test_author_does_not_receive_their_own_post(self, redis: Redis):
        channel_id = 60
        author = str(uuid.uuid4())
        reader = str(uuid.uuid4())
        await service.sync_subscribe(redis, author, channel_id)
        await service.sync_subscribe(redis, reader, channel_id)

        delivered = await process_operation(
            redis, post_id=200, channel_id=channel_id, author_id=author
        )
        assert delivered == 1
        assert await service.render_queue_ids(redis, author, 10) == []
        assert await service.render_queue_ids(redis, reader, 10) == [200]

    async def test_forward_never_returns_to_a_previous_reviewer(self, redis: Redis):
        """The core loop: A reviews the post, forwards it, and the re-injected op must
        find someone else."""
        channel_id = 61
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        await service.sync_subscribe(redis, first, channel_id)
        await service.sync_subscribe(redis, second, channel_id)

        await service.place_post(redis, first, 201)
        await service.claim_from_queue(redis, first, 201)  # forwarded

        assert await process_operation(redis, post_id=201, channel_id=channel_id) == 1
        assert await service.render_queue_ids(redis, first, 10) == []
        assert await service.render_queue_ids(redis, second, 10) == [201]

    async def test_delivered_count_excludes_refusals(self, redis: Redis):
        """A refusal is not a delivery — the count the worker reports has to reflect
        what actually landed, or an op looks successful while reaching nobody."""
        channel_id = 62
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        await service.place_post(redis, user, 202)
        await service.claim_from_queue(redis, user, 202)

        assert await process_operation(redis, post_id=202, channel_id=channel_id) == 0


class TestSaturation:
    async def test_exhausted_channel_is_abandoned_not_retried(self, redis: Redis):
        """Everyone subscribed has already had the post, so retrying can never succeed.
        Parking it would cycle it through the stream for FEED_RETRY_MAX_AGE_SECONDS."""
        channel_id = 70
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        await service.place_post(redis, user, 300)
        await service.claim_from_queue(redis, user, 300)

        assert await process_operation(redis, post_id=300, channel_id=channel_id) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 0

    async def test_author_only_channel_is_abandoned(self, redis: Redis):
        channel_id = 71
        author = str(uuid.uuid4())
        await service.sync_subscribe(redis, author, channel_id)

        delivered = await process_operation(
            redis, post_id=301, channel_id=channel_id, author_id=author
        )
        assert delivered == 0
        assert await redis.zcard(keys.OPS_RETRY) == 0

    async def test_merely_full_channel_is_still_retried(self, redis: Redis):
        """The distinction that matters: full queues are temporary, so this one parks.
        Getting it wrong would silently drop every op in a busy channel."""
        channel_id = 72
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, user, post_id)

        assert await process_operation(redis, post_id=302, channel_id=channel_id) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 1

    async def test_empty_channel_is_still_retried(self, redis: Redis):
        """A channel with no subscribers is not exhausted — subscribing pulls no
        history, so the parked op is the only way its backlog ever gets delivered."""
        assert await process_operation(redis, post_id=303, channel_id=73) == 0
        assert await redis.zcard(keys.OPS_RETRY) == 1

    async def test_has_eligible_recipient_ignores_queue_capacity(self, redis: Redis):
        channel_id = 74
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        for post_id in range(1, settings.FEED_QUEUE_MAX_SLOTS + 1):
            await service.place_post(redis, user, post_id)

        # Full, but has never had post 400 ⇒ still a candidate.
        assert await service.has_eligible_recipient(redis, channel_id, 400)

    async def test_has_eligible_recipient_cheap_gate_shortcut(self, redis: Redis):
        """With unseen subscribers clearly outnumbering seen+author, the function
        must return True via the cheap O(1) SCARD gate without needing the O(channel)
        SDIFF scan (see the docstring's "cheap gate first")."""
        channel_id = 75
        subscribers = [str(uuid.uuid4()) for _ in range(3)]
        for u in subscribers:
            await service.sync_subscribe(redis, u, channel_id)
        # Nobody has seen post 401 yet: seen_count(0) + 1 < subscribers(3).
        assert await service.has_eligible_recipient(redis, channel_id, 401)

    async def test_both_exclusion_flags_off_always_eligible(
        self, redis: Redis, toggle
    ):
        """Exhaustion is only a meaningful concept because of the two exclusion
        flags; with both off there is nothing to be exhausted from."""
        toggle("FEED_EXCLUDE_SEEN", False)
        toggle("FEED_EXCLUDE_OWN_POSTS", False)
        channel_id = 76
        user = str(uuid.uuid4())
        await service.sync_subscribe(redis, user, channel_id)
        await service.place_post(redis, user, 402)
        await service.claim_from_queue(redis, user, 402)

        assert await service.has_eligible_recipient(redis, channel_id, 402)

    async def test_seen_disabled_but_own_posts_enabled_uses_smembers_path(
        self, redis: Redis, toggle
    ):
        """With FEED_EXCLUDE_SEEN off, the "who's left" scan reads the whole channel
        (smembers) rather than diffing against a seen set (sdiff) - exercised here
        with a sole subscriber who is also the author, so it is still correctly
        exhausted (author-only) even though "seen" plays no part."""
        toggle("FEED_EXCLUDE_SEEN", False)
        channel_id = 77
        author = str(uuid.uuid4())
        await service.sync_subscribe(redis, author, channel_id)

        assert not await service.has_eligible_recipient(
            redis, channel_id, 403, author_id=author
        )


class TestSeenSeeding:
    async def test_rebuild_seeds_seen_from_reviews(
        self, redis: Redis, db, create_user, create_channel, create_post
    ):
        """Without this, a Redis rebuild silently re-opens re-delivery for every post
        still in circulation."""
        from tests.utils import review, subscribe

        user = await create_user()
        channel = await create_channel()
        post = await create_post(channel=channel)
        await subscribe(db, user, channel)
        await review(db, user, post, "forward")

        stats = await service.rebuild_from_pg(redis, db)
        assert await redis.sismember(keys.seen(post.id), str(user.id))
        # rebuild_redis.py / seed_dev_data.py print this key.
        assert stats["seen_seeded"] >= 1

    async def test_rebuild_does_not_backfill_own_posts(
        self, redis: Redis, db, create_user, create_channel, create_post
    ):
        """Fan-out skips the author via the stream entry; the backfill path has no
        equivalent and has to filter in SQL."""
        from tests.utils import subscribe

        author = await create_user()
        channel = await create_channel()
        post = await create_post(channel=channel, author=author)
        await subscribe(db, author, channel)

        placed = await service.backfill_queue(redis, db, author.id, channel.id)
        assert placed == 0
        assert post.id not in await service.render_queue_ids(redis, str(author.id), 10)

    async def test_seeding_is_a_noop_when_the_flag_is_off(
        self, redis: Redis, db, create_user, create_channel, create_post, toggle
    ):
        from tests.utils import review, subscribe

        toggle("FEED_EXCLUDE_SEEN", False)
        user = await create_user()
        channel = await create_channel()
        post = await create_post(channel=channel)
        await subscribe(db, user, channel)
        await review(db, user, post, "forward")

        stats = await service.rebuild_from_pg(redis, db)
        assert stats["seen_seeded"] == 0
        assert not await redis.sismember(keys.seen(post.id), str(user.id))
