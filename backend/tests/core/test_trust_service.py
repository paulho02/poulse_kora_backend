"""app.core.trust_service: the aggregates behind the score, and the cache over them.

tests/core/test_trust.py exercises the formula over made-up numbers. This covers the
part that turns a database into those numbers, which is where the subtler mistakes live:
a window boundary off by a day, a `lag()` that measures the gap to the wrong row, a
population rate computed from one reader's behaviour.

The window test is the one worth keeping honest. "Only the recent past counts" is the
whole reason an account cannot bank trust and coast on it, and the only way to see it
fail is to put a row on the far side of the boundary and check it is ignored.
"""

from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import trust, trust_service
from app.core.config import settings
from app.models.channel import Channel
from app.models.post import Post
from app.models.post_review import PostReview
from app.models.probe_response import ProbeResponse
from app.models.user import User


async def add_reviews(
    db: AsyncSession, user: User, posts: list[Post], *, kind: str, gap_seconds: float
):
    """`len(posts)` reviews for `user`, spaced `gap_seconds` apart inside the window."""
    start = trust_service.window_start() + timedelta(days=1)
    for index, post in enumerate(posts):
        db.add(
            PostReview(
                user_id=user.id,
                post_id=post.id,
                kind=kind,
                created=start + timedelta(seconds=index * gap_seconds),
            )
        )
    await db.commit()


class TestReviewAggregates:
    async def test_it_counts_reviews_and_forwards_in_the_window(
        self, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        posts = [await create_post(channel=channel) for _ in range(5)]
        await add_reviews(db, user, posts[:3], kind="forward", gap_seconds=60)
        await add_reviews(db, user, posts[3:], kind="drop", gap_seconds=60)

        reviews, forwards, _, _ = await trust_service._review_aggregates(
            db, str(user.id), trust_service.window_start()
        )

        assert reviews == 5
        assert forwards == 3

    async def test_anything_older_than_the_window_is_invisible(
        self, db: AsyncSession, create_user, create_channel, create_post
    ):
        """The entire decay mechanism. A reader who was busy two months ago and has
        done nothing since has to look exactly like a newcomer."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        old = trust_service.window_start() - timedelta(days=1)
        for _ in range(30):
            post: Post = await create_post(channel=channel)
            db.add(
                PostReview(
                    user_id=user.id, post_id=post.id, kind="forward", created=old
                )
            )
        await db.commit()

        reviews, forwards, paced, hasty = await trust_service._review_aggregates(
            db, str(user.id), trust_service.window_start()
        )

        assert (reviews, forwards, paced, hasty) == (0, 0, 0, 0)

    async def test_hasty_reviews_are_the_ones_that_followed_too_closely(
        self, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        posts = [await create_post(channel=channel) for _ in range(10)]
        await add_reviews(
            db,
            user,
            posts,
            kind="drop",
            gap_seconds=settings.TRUST_MIN_READ_SECONDS / 10,
        )

        reviews, _, paced, hasty = await trust_service._review_aggregates(
            db, str(user.id), trust_service.window_start()
        )

        assert reviews == 10
        # Nine gaps between ten reviews: the first has no predecessor to be measured
        # against, which is why `paced` is counted rather than assumed to be n - 1.
        assert paced == 9
        assert hasty == 9

    async def test_unhurried_reviewing_registers_none(
        self, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        posts = [await create_post(channel=channel) for _ in range(10)]
        await add_reviews(db, user, posts, kind="drop", gap_seconds=30)

        _, _, paced, hasty = await trust_service._review_aggregates(
            db, str(user.id), trust_service.window_start()
        )

        assert paced == 9
        assert hasty == 0

    async def test_another_readers_rows_are_not_counted(
        self, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        stranger: User = await create_user()
        channel: Channel = await create_channel()
        posts = [await create_post(channel=channel) for _ in range(4)]
        await add_reviews(db, stranger, posts, kind="forward", gap_seconds=60)

        reviews, _, _, _ = await trust_service._review_aggregates(
            db, str(user.id), trust_service.window_start()
        )

        assert reviews == 0


class TestProbeAggregates:
    async def test_it_counts_answers_and_correct_ones(
        self, db: AsyncSession, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        for index in range(4):
            post: Post = await create_post(channel=channel)
            db.add(
                ProbeResponse(
                    user_id=user.id,
                    post_id=post.id,
                    variant_code="x",
                    expected_kind="drop",
                    given_kind="drop" if index < 3 else "forward",
                    correct=index < 3,
                )
            )
        await db.commit()

        answered, correct = await trust_service._probe_aggregates(
            db, str(user.id), trust_service.window_start()
        )

        assert (answered, correct) == (4, 3)


class TestPopulationForwardRate:
    async def test_a_thin_sample_yields_no_signal(
        self, db: AsyncSession, redis: Redis, monkeypatch
    ):
        """A deployment too young to have a consensus has nothing to be anomalous
        against; None switches the skew signal off rather than substituting a guess.

        The threshold is raised rather than the table emptied: test data persists
        across a run (see the fixtures in conftest), so "not enough reviews exist" has
        to be expressed as a setting rather than assumed of the database."""
        monkeypatch.setattr(settings, "TRUST_FORWARD_RATE_MIN_SAMPLE", 10**9)

        assert await trust_service.population_forward_rate(db, redis) is None

    async def test_a_real_sample_yields_a_rate_between_zero_and_one(
        self, db: AsyncSession, redis: Redis, monkeypatch
    ):
        monkeypatch.setattr(settings, "TRUST_FORWARD_RATE_MIN_SAMPLE", 1)

        rate = await trust_service.population_forward_rate(db, redis)

        assert rate is not None
        assert 0.0 <= rate <= 1.0

    async def test_it_is_cached_across_readers(
        self, db: AsyncSession, redis: Redis
    ):
        """One number shared by everyone, and the only aggregate here that reads other
        accounts' rows - so it is cached far longer than an individual score."""
        await trust_service.population_forward_rate(db, redis)

        assert await redis.exists(trust_service.FORWARD_RATE_KEY) == 1
        ttl = await redis.ttl(trust_service.FORWARD_RATE_KEY)
        assert 0 < ttl <= settings.TRUST_FORWARD_RATE_TTL_SECONDS


class TestCaching:
    async def test_a_new_reader_is_neutral_and_gets_the_standard_fanout(
        self, db: AsyncSession, redis: Redis, create_user
    ):
        """The day this ships, every existing account is in exactly this state - which
        is what makes the deploy invisible rather than a step change in everyone's
        reach."""
        user: User = await create_user()

        result = await trust_service.reviewer_trust(db, redis, str(user.id))

        assert result.band == trust.BAND_NORMAL
        assert result.fanout == settings.FEED_FANOUT
        assert result.window_days == settings.TRUST_WINDOW_DAYS

    async def test_the_score_is_cached_and_invalidation_clears_it(
        self, db: AsyncSession, redis: Redis, create_user
    ):
        user: User = await create_user()
        await trust_service.reviewer_trust(db, redis, str(user.id))
        assert await redis.exists(trust_service.cache_key(str(user.id))) == 1

        await trust_service.invalidate(redis, str(user.id))

        assert await redis.exists(trust_service.cache_key(str(user.id))) == 0

    async def test_bands_are_recomputed_from_the_cached_score(
        self, db: AsyncSession, redis: Redis, monkeypatch, create_user
    ):
        """Only the score is cached, so retuning the band thresholds or FEED_FANOUT
        takes effect on deploy instead of trickling in as entries expire."""
        user: User = await create_user()
        await trust_service.reviewer_trust(db, redis, str(user.id))

        monkeypatch.setattr(settings, "TRUST_BAND_HIGH_MIN", 10)
        result = await trust_service.reviewer_trust(db, redis, str(user.id))

        assert result.band == trust.BAND_HIGH

    async def test_disabling_trust_stops_the_forward_path_asking(
        self, db: AsyncSession, redis: Redis, monkeypatch, create_user
    ):
        """None means no `fanout` field on the stream entry at all, which is what
        makes the disabled path identical to the pre-trust one."""
        user: User = await create_user()
        monkeypatch.setattr(settings, "TRUST_ENABLED", False)

        assert await trust_service.forward_fanout(db, redis, str(user.id)) is None
