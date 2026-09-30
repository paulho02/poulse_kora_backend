"""Test posts end to end: minting them, answering them, and what they must not touch.

The interesting assertions here are the negative ones. A probe is a *measurement*, and
nearly every bug this feature could have is the measurement leaking into the thing it
measures - a `PostReview` row that feeds the pace signal back into itself, a counter
that makes the profile claim a probe was a review, a fan-out operation that spends
someone's reach on a post nobody else should ever see.

The one deliberate exception is the token, and it is asserted as loudly as the rest:
answering a probe pays, because the attention it cost was real and a reader who happens
to be measured more often should not end the month poorer for it.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import probes, trust_service
from app.core.config import settings
from app.core.probe_templates import VARIANTS
from app.feed import service
from app.models.channel import Channel
from app.models.post import Post
from app.models.post_block import PostBlock
from app.models.post_review import PostReview
from app.models.probe_response import ProbeResponse
from app.models.user import User
from tests.utils import get_jwt_header, subscribe


async def mint_for(db: AsyncSession, redis: Redis, user: User, channel: Channel):
    """Give `user` a probe in `channel`, the way a review would have."""
    await subscribe(db, user, channel)
    await db.refresh(user)
    return await probes.mint_probe(db, redis, user)


async def answer(client: AsyncClient, user: User, post: Post, kind: str):
    return await client.post(
        settings.API_PATH + f"/posts/{post.id}/review",
        headers=get_jwt_header(user),
        json={"kind": kind},
    )


async def expected_kind_of(db: AsyncSession, post: Post) -> str:
    variant = await probes.variant_for_post(db, post)
    assert variant is not None
    return variant.expected_kind


def other(kind: str) -> str:
    return "drop" if kind == "forward" else "forward"


class TestMinting:
    async def test_a_probe_lands_in_the_queue_looking_like_a_post(
        self, db: AsyncSession, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()

        post = await mint_for(db, redis, user, channel)

        assert post is not None
        assert post.is_probe is True
        assert post.channel_id == channel.id
        assert post.language in user.content_languages
        assert await service.render_queue_ids(redis, str(user.id), 10) == [post.id]

    async def test_minting_costs_the_economy_nothing(
        self, db: AsyncSession, redis: Redis, create_user, create_channel
    ):
        """A probe is placed straight into one reader's queue, never fanned out. If it
        minted an operation it would count as congestion and push the admission price
        up - charging authors for the privilege of having readers measured."""
        user: User = await create_user()
        channel: Channel = await create_channel()

        await mint_for(db, redis, user, channel)

        assert await service.operation_queue_len(redis) == 0
        assert await service.outstanding_ops_total(redis) == 0

    async def test_it_is_visible_in_the_feed_and_marked_as_a_test(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """The marker is the honest disclosure: a reader is entitled to a fair chance
        of recognising that they are being measured."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )

        assert resp.status_code == 200, resp.text
        entry = next(e for e in resp.json() if e["post_id"] == post.id)
        assert entry["post"]["is_probe"] is True
        assert entry["post"]["blocks"][0]["text"]

    async def test_no_subscriptions_means_no_probe(
        self, db: AsyncSession, redis: Redis, create_user
    ):
        """Skipped rather than forced into some channel: a probe has to arrive
        somewhere the reader would plausibly have got a post."""
        user: User = await create_user()
        assert await probes.mint_probe(db, redis, user) is None

    async def test_a_full_queue_means_no_probe(
        self, db: AsyncSession, redis: Redis, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        for _ in range(settings.FEED_QUEUE_MAX_SLOTS):
            post: Post = await create_post(channel=channel)
            await service.place_post(redis, str(user.id), post.id)

        assert await probes.mint_probe(db, redis, user) is None

    async def test_a_language_with_no_copy_is_not_probed(
        self, db: AsyncSession, redis: Redis, monkeypatch,
        create_user, create_channel,
    ):
        """Serving English text under another language's label would be exactly the
        mislabelling the language economy exists to correct."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        user.content_languages = ["xx"]
        await db.commit()

        assert await probes.mint_probe(db, redis, user) is None


class TestShouldMint:
    async def test_an_outstanding_probe_blocks_the_next(
        self, redis: Redis, create_user, monkeypatch
    ):
        user: User = await create_user()
        monkeypatch.setattr(settings, "TRUST_PROBE_RATE", 1.0)
        await redis.set(probes._pending_key(str(user.id)), 1)

        for _ in range(20):
            assert await probes.should_mint(redis, str(user.id)) is False

    async def test_the_gap_holds_off_back_to_back_probes(
        self, redis: Redis, create_user, monkeypatch
    ):
        """Even at a rate of 1.0 - two tests in a row is both irritating and the
        fastest way to teach someone what one looks like."""
        user: User = await create_user()
        monkeypatch.setattr(settings, "TRUST_PROBE_RATE", 1.0)

        results = [
            await probes.should_mint(redis, str(user.id))
            for _ in range(settings.TRUST_PROBE_MIN_GAP_REVIEWS)
        ]

        assert results[:-1] == [False] * (settings.TRUST_PROBE_MIN_GAP_REVIEWS - 1)
        assert results[-1] is True

    async def test_rate_zero_disables_minting(
        self, redis: Redis, create_user, monkeypatch
    ):
        user: User = await create_user()
        monkeypatch.setattr(settings, "TRUST_PROBE_RATE", 0.0)
        for _ in range(50):
            assert await probes.should_mint(redis, str(user.id)) is False


class TestAnsweringLeavesNoTrace:
    @pytest.mark.parametrize("kind", ["forward", "drop"])
    async def test_no_review_row_and_no_counters_move(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, kind,
    ):
        """The whole integrity of the score rests on this. A `PostReview` row would
        feed the probe back into the volume, pace and forward-rate signals it exists to
        calibrate, and would put a test post in the reader's review history."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)

        resp = await answer(client, user, post, kind)

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_probe"] is True
        reviews = await db.scalar(
            select(func.count(PostReview.id)).where(PostReview.user_id == user.id)
        )
        assert reviews == 0
        await db.refresh(user)
        assert (user.reviewed_count, user.forwarded_count, user.dropped_count) == (
            0,
            0,
            0,
        )
        await db.refresh(post)
        assert (post.forwarded_count, post.dropped_count) == (0, 0)

    async def test_forwarding_one_mints_no_fanout(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """A probe exists for one reader. Propagating it would hand strangers a post
        that is measuring somebody else - and would score them on it."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)

        await answer(client, user, post, "forward")

        assert await service.operation_queue_len(redis) == 0

    async def test_it_frees_the_queue_slot(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)

        await answer(client, user, post, "drop")

        assert await service.render_queue_ids(redis, str(user.id), 10) == []

    async def test_it_never_appears_in_the_review_history(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)
        await answer(client, user, post, "forward")

        resp = await client.get(
            settings.API_PATH + "/posts/reviewed", headers=get_jwt_header(user)
        )

        assert resp.status_code == 200, resp.text
        assert [e["post"]["id"] for e in resp.json()] == []

    async def test_the_score_badge_is_withheld(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """"1 of 1" would be a true number that means nothing - nobody else will ever
        see this post. The client shows the probe confirmation instead."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)

        data = (await answer(client, user, post, "forward")).json()

        assert data["post_forwarded_count"] == 0
        assert data["post_reviewed_count"] == 0


class TestAnsweringIsRecorded:
    async def test_the_token_is_paid(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """The one deliberate exception to "a probe changes nothing": the attention it
        cost was real, and being measured must not leave a reader poorer."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)

        data = (await answer(client, user, post, "drop")).json()

        assert data["token_balance"] == 1
        assert await service.token_balance(redis, str(user.id)) == 1

    async def test_a_gift_is_refused_and_the_probe_stays_answerable(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """A probe's author is the system; there is nobody to reward."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward", "gift_token": True},
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "gift_not_allowed"
        assert (await answer(client, user, post, "forward")).status_code == 200

    async def test_a_correct_answer_is_recorded_and_reported(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)
        kind = await expected_kind_of(db, post)

        data = (await answer(client, user, post, kind)).json()

        assert data["probe_correct"] is True
        response = await db.scalar(
            select(ProbeResponse).where(ProbeResponse.post_id == post.id)
        )
        assert response is not None
        assert response.correct is True
        assert response.given_kind == kind
        assert response.expected_kind == kind

    async def test_a_wrong_answer_is_told_to_the_reader(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """Silence would mean the only feedback a careless reader ever gets is reach
        quietly disappearing."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)
        kind = other(await expected_kind_of(db, post))

        data = (await answer(client, user, post, kind)).json()

        assert data["probe_correct"] is False

    async def test_an_unrecognisable_probe_is_not_scored(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """Variant copy gets edited, which orphans probes already in flight. Guessing
        what such a post used to ask would be worse than dropping the measurement -
        the reader still earns their token and still clears the slot."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)
        await db.execute(
            update(PostBlock)
            .where(PostBlock.post_id == post.id)
            .values(text="something nobody wrote")
        )
        await db.commit()

        data = (await answer(client, user, post, "drop")).json()

        assert data["is_probe"] is True
        assert data["probe_correct"] is None
        assert data["token_balance"] == 1
        assert await db.scalar(
            select(func.count(ProbeResponse.id)).where(
                ProbeResponse.post_id == post.id
            )
        ) == 0


class TestScoreEffect:
    async def test_answering_moves_the_score_immediately(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """The cached score is invalidated on answer rather than left to expire: this
        is the strongest and rarest input, and a reader should see the effect while
        they still remember answering."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        await db.refresh(user)
        before = await trust_service.reviewer_trust(db, redis, str(user.id))

        for _ in range(len(VARIANTS)):
            post = await probes.mint_probe(db, redis, user)
            if post is None:
                break
            await answer(client, user, post, other(await expected_kind_of(db, post)))

        after = await trust_service.reviewer_trust(db, redis, str(user.id))
        assert after.score < before.score

    async def test_getting_them_all_right_earns_the_wider_reach(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        await db.refresh(user)

        for _ in range(settings.TRUST_PROBE_CONFIDENCE_N):
            post = await probes.mint_probe(db, redis, user)
            assert post is not None
            await answer(client, user, post, await expected_kind_of(db, post))

        trust = await trust_service.reviewer_trust(db, redis, str(user.id))
        assert trust.band == "high"
        assert trust.fanout > settings.FEED_FANOUT


class TestProbesDoNotExpire:
    """A probe waits as long as the reader does, and counts in full when answered.

    This is why there is no cleanup job (see the note in app/core/probes.py). The score
    reads `ProbeResponse.created` - when it was *answered* - while the post carries
    `Post.created`, when it was minted, and those two can be arbitrarily far apart. Any
    age-based deletion keyed on the post would silently discard live evidence, and any
    deletion at all could hit a probe still sitting in a queue.
    """

    async def test_an_old_unanswered_probe_is_still_in_the_queue(
        self, db: AsyncSession, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)
        # Backdated well past any window someone might be tempted to prune on.
        await db.execute(
            update(Post)
            .where(Post.id == post.id)
            .values(created=datetime.now(timezone.utc) - timedelta(days=120))
        )
        await db.commit()

        assert post.id in await service.render_queue_ids(redis, str(user.id), 50)

    async def test_answering_an_old_probe_counts_in_full(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post = await mint_for(db, redis, user, channel)
        await db.execute(
            update(Post)
            .where(Post.id == post.id)
            .values(created=datetime.now(timezone.utc) - timedelta(days=120))
        )
        await db.commit()
        kind = await expected_kind_of(db, post)

        data = (await answer(client, user, post, kind)).json()

        assert data["probe_correct"] is True
        response = await db.scalar(
            select(ProbeResponse).where(ProbeResponse.post_id == post.id)
        )
        # Stamped now, not at mint time - so it sits inside the trust window however
        # long the post itself waited to be read.
        assert response.created >= trust_service.window_start()
        answered, correct = await trust_service._probe_aggregates(
            db, str(user.id), trust_service.window_start()
        )
        assert (answered, correct) == (1, 1)


class TestProbeAuthorIdentity:
    async def test_an_active_account_at_the_probe_address_is_never_the_author(
        self, db: AsyncSession, create_user, monkeypatch
    ):
        """The author is looked up by email *and* `is_active = false`. An active
        account at that address is somebody's (squatted before the row existed);
        publishing every test post under their name would be the worst outcome, so
        the lookup passes it by and the insert then fails on the unique email -
        no probe rather than a hijacked one."""
        squatter: User = await create_user()
        monkeypatch.setattr(settings, "TRUST_PROBE_AUTHOR_EMAIL", squatter.email)
        monkeypatch.setattr(
            settings, "TRUST_PROBE_AUTHOR_USERNAME", f"p{squatter.id.hex[:12]}"
        )

        with pytest.raises(IntegrityError):
            await probes._ensure_probe_author(db)
        await db.rollback()

        await db.refresh(squatter)
        assert squatter.is_active
        assert squatter.username != settings.TRUST_PROBE_AUTHOR_USERNAME

    async def test_the_author_is_an_inactive_identity(
        self, db: AsyncSession, monkeypatch
    ):
        suffix = uuid.uuid4().hex[:8]
        monkeypatch.setattr(
            settings,
            "TRUST_PROBE_AUTHOR_EMAIL",
            f"probes-{suffix}@peerkola.example.com",
        )
        monkeypatch.setattr(
            settings, "TRUST_PROBE_AUTHOR_USERNAME", f"peerkola{suffix}"
        )
        author = await probes._ensure_probe_author(db)
        assert author.is_active is False
        assert author.is_verified is True
        # Idempotent: the second call finds the same row.
        again = await probes._ensure_probe_author(db)
        assert again.id == author.id
