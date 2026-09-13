"""Language as the other half of the routing key.

A post declares one language; a reader accepts a set; fan-out samples an audience set
that is already the intersection of the two (`keys.audience`), so the delivery path
pays nothing for the filtering. These tests cover the three places that can go wrong:
which audience an op samples, whether "no language" really reaches everyone, and
whether exhaustion is judged against the route or the channel.
"""

import json
import time
import uuid

import pytest
from redis.asyncio import Redis

from app.core.config import settings
from app.core.languages import UNSPECIFIED
from app.feed import keys, service
from app.feed.worker import consume_once, process_operation


@pytest.fixture
def toggle(monkeypatch):
    """Flip a FEED_* flag for one test (settings is a module-level singleton)."""

    def inner(name: str, value):
        monkeypatch.setattr(settings, name, value)

    return inner


class TestAudienceKey:
    async def test_a_language_post_reaches_only_readers_of_that_language(
        self, redis: Redis
    ):
        channel_id = 1200
        german = str(uuid.uuid4())
        english = str(uuid.uuid4())
        await service.sync_subscribe(redis, german, channel_id, ["de"])
        await service.sync_subscribe(redis, english, channel_id, ["en"])

        delivered = await process_operation(
            redis, post_id=1201, channel_id=channel_id, language="de"
        )

        assert delivered == 1
        assert await service.render_queue_ids(redis, german, 10) == [1201]
        assert await service.render_queue_ids(redis, english, 10) == []

    async def test_a_reader_of_both_languages_is_in_both_audiences(self, redis: Redis):
        channel_id = 1210
        bilingual = str(uuid.uuid4())
        await service.sync_subscribe(redis, bilingual, channel_id, ["en", "de"])

        await process_operation(
            redis, post_id=1211, channel_id=channel_id, language="de"
        )
        await process_operation(
            redis, post_id=1212, channel_id=channel_id, language="en"
        )

        assert set(await service.render_queue_ids(redis, bilingual, 10)) == {1211, 1212}

    async def test_unspecified_reaches_every_subscriber(self, redis: Redis):
        """The channel set is exactly the union of its language slices, so a post with
        no language needs no audience set of its own - and it stays deliverable to a
        reader whose own language has almost no supply, which is why the widest-reaching
        content being the cheapest is a feature rather than an arbitrage."""
        channel_id = 1220
        readers = [str(uuid.uuid4()) for _ in range(3)]
        for reader, langs in zip(readers, (["en"], ["de"], ["en", "de"]), strict=True):
            await service.sync_subscribe(redis, reader, channel_id, langs)

        delivered = await process_operation(
            redis, post_id=1221, channel_id=channel_id, language=UNSPECIFIED
        )

        assert delivered == settings.FEED_FANOUT
        for reader in readers:
            assert await service.render_queue_ids(redis, reader, 10) == [1221]

    async def test_a_language_nobody_reads_finds_no_audience(self, redis: Redis):
        channel_id = 1230
        await service.sync_subscribe(redis, str(uuid.uuid4()), channel_id, ["en"])

        delivered = await process_operation(
            redis, post_id=1231, channel_id=channel_id, language="de"
        )

        assert delivered == 0


class TestRouteExhaustion:
    async def test_exhaustion_is_judged_against_the_route_not_the_channel(
        self, redis: Redis, toggle
    ):
        """The most dangerous way to get this wrong. If `has_eligible_recipient` asked
        the channel set, a German post whose German readers had all seen it would see
        the channel's English subscribers, conclude it still had an audience, and park
        itself for FEED_RETRY_MAX_AGE_SECONDS - inflating the price of every post in
        the channel to chase readers it can never be delivered to."""
        toggle("FEED_EXCLUDE_SEEN", True)
        channel_id = 1240
        german = str(uuid.uuid4())
        await service.sync_subscribe(redis, german, channel_id, ["de"])
        for _ in range(5):
            await service.sync_subscribe(redis, str(uuid.uuid4()), channel_id, ["en"])

        # The channel's only German reader has now had it.
        await service.place_post(redis, german, 1241)
        await service.claim_from_queue(redis, german, 1241)

        delivered = await process_operation(
            redis, post_id=1241, channel_id=channel_id, language="de"
        )

        assert delivered == 0
        # Abandoned, not parked: the English subscribers are not this post's audience.
        assert await redis.zcard(keys.OPS_RETRY) == 0

    async def test_an_empty_language_route_is_still_parked(self, redis: Redis):
        """Strictly narrower than exhaustion: nobody reads German in this channel
        *yet*, and that backlog is how the first German reader to arrive gets something
        to read - the same reason a brand-new channel's ops are parked."""
        channel_id = 1250
        await service.sync_subscribe(redis, str(uuid.uuid4()), channel_id, ["en"])

        delivered = await process_operation(
            redis, post_id=1251, channel_id=channel_id, language="de"
        )

        assert delivered == 0
        assert await redis.zcard(keys.OPS_RETRY) == 1

    async def test_a_parked_language_op_delivers_once_a_reader_opts_in(
        self, redis: Redis
    ):
        channel_id = 1260
        await service.enqueue_operation(redis, 1261, channel_id, "de")
        await consume_once(redis, "test-lang", timeout=2.0)
        assert await redis.zcard(keys.OPS_RETRY) == 1

        reader = str(uuid.uuid4())
        await service.sync_subscribe(redis, reader, channel_id, ["de"])
        # Past the retry delay but well inside FEED_RETRY_MAX_AGE_SECONDS, or the op
        # would be abandoned as past its deadline instead of re-added.
        await service.reschedule_due_retries(redis, now=time.time() + 3600)
        await consume_once(redis, "test-lang", timeout=2.0)

        assert await service.render_queue_ids(redis, reader, 10) == [1261]


class TestOutstandingOpsAreRoutedSeparately:
    async def test_two_languages_in_one_channel_count_apart(self, redis: Redis):
        """A channel's German slice can be congested while its English slice has room;
        one counter across both would price each by the other's backlog."""
        channel_id = 1270
        await service.enqueue_operation(redis, 1271, channel_id, "de")
        await service.enqueue_operation(redis, 1272, channel_id, "de")
        await service.enqueue_operation(redis, 1273, channel_id, "en")

        counts = await service.route_outstanding_ops(
            redis, [(channel_id, "de"), (channel_id, "en")]
        )

        assert counts == {(channel_id, "de"): 2, (channel_id, "en"): 1}


class TestBackwardCompatibility:
    async def test_an_op_without_a_language_routes_through_the_whole_channel(
        self, redis: Redis
    ):
        """Ops already on the stream when routing was deployed carry no language.
        UNSPECIFIED is the honest reading - it is the audience they were minted
        against - so a backlog crossing the deploy is delivered as intended rather than
        narrowed to one language's readers."""
        channel_id = 1280
        reader = str(uuid.uuid4())
        await service.sync_subscribe(redis, reader, channel_id, ["de"])
        # Exactly what `_add_to_stream` wrote before the field existed.
        await redis.xadd(
            keys.STREAM, {"post_id": "1281", "channel_id": str(channel_id)}
        )

        await consume_once(redis, "test-legacy", timeout=2.0)

        assert await service.render_queue_ids(redis, reader, 10) == [1281]

    async def test_a_parked_op_without_a_language_survives_the_deploy(
        self, redis: Redis
    ):
        channel_id = 1290
        payload = json.dumps({"post_id": 1291, "channel_id": channel_id})
        await redis.zadd(keys.OPS_RETRY, {f"{uuid.uuid4().hex}:{payload}": 0})

        assert await service.reschedule_due_retries(redis, now=1) == 1

        entries = await redis.xrange(keys.STREAM)
        assert entries[-1][1]["language"] == UNSPECIFIED


class TestContentLanguageSync:
    async def test_adding_a_language_joins_that_audience(self, redis: Redis):
        channel_id = 1300
        reader = str(uuid.uuid4())
        await service.sync_subscribe(redis, reader, channel_id, ["en"])

        await service.sync_content_languages(redis, reader, [channel_id], ["en", "de"])

        assert await redis.sismember(keys.audience(channel_id, "de"), reader)
        assert await redis.sismember(keys.audience(channel_id, "en"), reader)

    async def test_dropping_a_language_leaves_that_audience(self, redis: Redis):
        channel_id = 1310
        reader = str(uuid.uuid4())
        await service.sync_subscribe(redis, reader, channel_id, ["en", "de"])

        await service.sync_content_languages(redis, reader, [channel_id], ["de"])

        assert not await redis.sismember(keys.audience(channel_id, "en"), reader)
        assert await redis.sismember(keys.audience(channel_id, "de"), reader)
        # Still in the channel itself - that is subscription, not language, so
        # UNSPECIFIED posts keep arriving.
        assert await redis.sismember(keys.channel(channel_id), reader)

    async def test_membership_total_tracks_the_change(self, redis: Redis):
        channel_id = 1320
        reader = str(uuid.uuid4())
        await service.sync_subscribe(redis, reader, channel_id, ["en"])
        assert await service.subscription_total(redis) == 2  # channel + en

        await service.sync_content_languages(redis, reader, [channel_id], ["en", "de"])
        assert await service.subscription_total(redis) == 3

        await service.sync_content_languages(redis, reader, [channel_id], ["de"])
        assert await service.subscription_total(redis) == 2

    async def test_is_idempotent(self, redis: Redis):
        """Writes the absolute set rather than a diff, so re-running repairs drift
        instead of compounding it - which is what lets the route re-run it even when
        the stored value did not change."""
        channel_id = 1330
        reader = str(uuid.uuid4())
        await service.sync_subscribe(redis, reader, channel_id, ["en"])
        before = await service.subscription_total(redis)

        for _ in range(3):
            await service.sync_content_languages(redis, reader, [channel_id], ["en"])

        assert await service.subscription_total(redis) == before

    async def test_no_channels_is_a_noop(self, redis: Redis):
        reader = str(uuid.uuid4())
        await service.sync_content_languages(redis, reader, [], ["en"])
        assert await service.subscription_total(redis) == 0


class TestBackfillRespectsLanguage:
    async def test_backfill_skips_posts_in_unaccepted_languages(
        self, redis: Redis, db, create_user, create_channel, create_post
    ):
        """Live delivery samples an audience set that is already correct; this path
        queries Postgres directly and has no such set to lean on, so a missing filter
        here would undo the routing for exactly the users a rebuild is meant to
        restore."""
        user = await create_user()
        channel = await create_channel()
        german = await create_post(channel=channel, language="de")
        english = await create_post(channel=channel, language="en")
        universal = await create_post(channel=channel, language=UNSPECIFIED)

        count = await service.backfill_queue(redis, db, user.id, channel.id, ["de"])

        assert count == 2
        queued = set(await service.render_queue_ids(redis, str(user.id), 10))
        assert queued == {german.id, universal.id}
        assert english.id not in queued
