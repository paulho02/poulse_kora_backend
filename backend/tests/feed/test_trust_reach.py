"""What a forward is worth: Reviewer Trust, carried onto the stream entry and honoured.

The worker never opens a Postgres session, so it cannot score anyone - the recipient
count has to travel with the operation, the same way `author_id` and `language` do. That
makes three things worth pinning: the field is written, it is honoured, and its absence
degrades to the standard fan-out rather than to zero recipients.

The last one is the deploy-safety property. Operations already on the stream or parked
in `ops:retry` when this landed carry no `fanout`, and they have to keep fanning out at
the reach they were minted under.
"""

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import trust
from app.core.config import settings
from app.feed import keys, service
from app.feed.worker import process_operation
from app.models.channel import Channel
from app.models.post import Post
from app.models.user import User
from tests.utils import get_jwt_header


async def audience(redis: Redis, channel: Channel, size: int) -> list[str]:
    """`size` subscribers of `channel`, all with room in their queues."""
    import uuid

    user_ids = [str(uuid.uuid4()) for _ in range(size)]
    await redis.sadd(keys.channel(channel.id), *user_ids)
    await redis.sadd(keys.FREE_QUEUE, *user_ids)
    return user_ids


class TestFanoutTravelsOnTheEntry:
    async def test_the_field_is_written_and_read_back(self, redis: Redis):
        await service.enqueue_operation(redis, 1, 2, "en", fanout=7)

        entries = await redis.xrange(keys.STREAM)
        assert entries[0][1]["fanout"] == "7"

    async def test_no_field_is_written_when_none_is_given(self, redis: Redis):
        """An original post, or a deployment with trust off, must produce an entry
        byte-identical to a pre-trust one."""
        await service.enqueue_operation(redis, 1, 2, "en")

        entries = await redis.xrange(keys.STREAM)
        assert "fanout" not in entries[0][1]

    async def test_a_parked_op_keeps_the_reach_it_was_minted_with(
        self, redis: Redis
    ):
        """A reader's verdict is worth what their judgement was worth when they made
        it, not what it has drifted to over the ten days the post spent looking for an
        audience."""
        await service.schedule_retry(redis, 1, 2, "en", delay=-1, fanout=5)

        await service.reschedule_due_retries(redis)

        entries = await redis.xrange(keys.STREAM)
        assert entries[0][1]["fanout"] == "5"


class TestTheWorkerHonoursIt:
    @pytest.mark.parametrize("fanout", [1, 2, 5])
    async def test_it_delivers_exactly_that_many(
        self, redis: Redis, create_channel, create_post, fanout
    ):
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await audience(redis, channel, 12)

        delivered = await process_operation(
            redis, post.id, channel.id, "und", fanout=fanout
        )

        assert delivered == fanout

    async def test_a_missing_fanout_falls_back_to_the_standard_one(
        self, redis: Redis, create_channel, create_post
    ):
        """The degradation path for every op minted before this existed."""
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await audience(redis, channel, 12)

        delivered = await process_operation(redis, post.id, channel.id, "und")

        assert delivered == settings.FEED_FANOUT

    async def test_a_nonsense_value_still_reaches_someone(
        self, redis: Redis, create_channel, create_post
    ):
        """Clamped rather than trusted: a zero or negative value on an entry would turn
        the operation into a no-op that still retires, losing the forward silently."""
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await audience(redis, channel, 12)

        delivered = await process_operation(
            redis, post.id, channel.id, "und", fanout=0
        )

        assert delivered == 1


class TestReviewPathAppliesTheBand:
    async def test_a_forward_carries_the_forwarders_band(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        """A brand-new reader has no evidence either way, so they sit in the normal
        band and their forward is worth the standard fan-out - which is what makes this
        feature invisible on the day it ships."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )

        assert resp.status_code == 200, resp.text
        entries = await redis.xrange(keys.STREAM)
        assert entries[0][1]["fanout"] == str(
            trust.fanout_for(trust.BAND_NORMAL, settings)
        )

    async def test_trust_disabled_writes_no_field_at_all(
        self, client: AsyncClient, db: AsyncSession, redis: Redis, monkeypatch,
        create_user, create_channel, create_post,
    ):
        monkeypatch.setattr(settings, "TRUST_ENABLED", False)
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )

        entries = await redis.xrange(keys.STREAM)
        assert "fanout" not in entries[0][1]
