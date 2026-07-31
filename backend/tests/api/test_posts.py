from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.feed import keys, service
from app.feed.worker import consume_once
from app.models.channel import Channel
from app.models.post import Post
from app.models.user import User
from tests.utils import get_jwt_header, grant_subscription, subscribe


class TestPostsFeed:
    async def test_feed_empty_with_empty_queue(
        self, client: AsyncClient, create_user
    ):
        user: User = await create_user()
        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_feed_returns_queued_posts(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post_a: Post = await create_post(channel=channel)
        post_b: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post_a.id)
        await service.place_post(redis, str(user.id), post_b.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        ids = [p["id"] for p in resp.json()]
        # LPUSH ⇒ most recently placed is at the head.
        assert ids == [post_b.id, post_a.id]

    async def test_feed_anonymous_post_hides_author_for_other_users(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        viewer: User = await create_user()
        channel: Channel = await create_channel()
        author: User = await create_user()
        post: Post = await create_post(channel=channel, author=author, is_anonymous=True)
        await service.place_post(redis, str(viewer.id), post.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed", headers=get_jwt_header(viewer)
        )
        assert resp.status_code == 200, resp.text
        [data] = [p for p in resp.json() if p["id"] == post.id]
        assert data["author"]["id"] is None
        assert data["author"]["username"] is None

    async def test_feed_filtered_by_channel_id(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
    ):
        user: User = await create_user()
        channel_a: Channel = await create_channel()
        channel_b: Channel = await create_channel()
        post_a: Post = await create_post(channel=channel_a)
        post_b: Post = await create_post(channel=channel_b)
        await service.place_post(redis, str(user.id), post_a.id)
        await service.place_post(redis, str(user.id), post_b.id)

        resp = await client.get(
            settings.API_PATH + "/posts/feed",
            params={"channel_id": channel_a.id},
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        ids = [p["id"] for p in resp.json()]
        assert ids == [post_a.id]


class TestCreatePost:
    async def test_create_fails_insufficient_tokens(
        self, client: AsyncClient, create_user, create_channel
    ):
        user: User = await create_user()  # starts with 0 tokens
        channel: Channel = await create_channel()

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "hi"},
        )
        assert resp.status_code == 402, resp.text
        body = resp.json()["detail"]
        assert body["error"] == "insufficient_tokens"
        assert body["balance"] == 0
        assert body["price"] >= settings.FEED_PRICE_MIN

    async def test_create_succeeds_with_tokens_and_enqueues_op(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "unlocked post"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["post"]["text"] == "unlocked post"
        assert data["price"] >= settings.FEED_PRICE_MIN
        assert data["token_balance"] == settings.FEED_PRICE_MAX - data["price"]
        # The new post is queued as an operation for the worker to distribute.
        assert await service.operation_queue_len(redis) == 1

    async def test_superuser_posts_for_free(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()

        channel: Channel = await create_channel()
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "superuser post"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["token_balance"] == 0  # nothing spent, nothing earned

    async def test_create_channel_404(
        self, client: AsyncClient, redis: Redis, create_user
    ):
        user: User = await create_user()
        await service.earn_token(redis, str(user.id), 10)
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": 10**6, "text": "hi"},
        )
        assert resp.status_code == 404

    async def test_create_post_snapshots_supporter_subscription(
        self, client: AsyncClient, redis: Redis, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        await grant_subscription(db, user, "supporter")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "supporter post"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["post"]["subscription_kind"] == "supporter"

    async def test_create_post_without_subscription_has_no_badge(
        self, client: AsyncClient, redis: Redis, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "free post"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["post"]["subscription_kind"] is None

    async def test_post_keeps_supporter_badge_after_subscription_revoked(
        self, client: AsyncClient, redis: Redis, db: AsyncSession, create_user, create_channel
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, user, channel)
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        subscription = await grant_subscription(db, user, "supporter")

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=get_jwt_header(user),
            json={"channel_id": channel.id, "text": "supporter post"},
        )
        assert resp.status_code == 201, resp.text
        post_id = resp.json()["post"]["id"]

        await db.delete(subscription)
        await db.commit()

        resp = await client.get(
            settings.API_PATH + f"/posts/{post_id}",
            headers=get_jwt_header(user),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["subscription_kind"] == "supporter"


class TestPostEconomy:
    async def test_economy_returns_balance_and_price(
        self, client: AsyncClient, redis: Redis, create_user
    ):
        user: User = await create_user()
        await service.earn_token(redis, str(user.id), 3)

        resp = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["token_balance"] == 3
        assert data["post_price"] >= settings.FEED_PRICE_MIN
        assert "post_price_expires_at" in data

    async def test_economy_price_is_shared_until_expiry(
        self, client: AsyncClient, redis: Redis, create_user
    ):
        """Two reads must agree, even if congestion changes in between — the whole
        point of the shared snapshot is that clients never see the price drift
        between calls a few seconds apart."""
        user: User = await create_user()

        resp1 = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )
        data1 = resp1.json()

        for i in range(settings.FEED_PRICE_STEP_ITEMS * 3):
            await service.enqueue_operation(redis, post_id=i, channel_id=1)

        resp2 = await client.get(
            settings.API_PATH + "/posts/economy", headers=get_jwt_header(user)
        )
        data2 = resp2.json()

        assert data2["post_price"] == data1["post_price"]
        assert data2["post_price_expires_at"] == data1["post_price_expires_at"]


class TestGetPost:
    async def test_get_post_404_for_non_subscriber(
        self, client: AsyncClient, create_user, create_post
    ):
        user: User = await create_user()
        post: Post = await create_post()
        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(user)
        )
        assert resp.status_code == 404

    async def test_get_post_visible_to_author_even_if_anonymous(
        self, client: AsyncClient, db: AsyncSession, create_user, create_channel, create_post
    ):
        author: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, author, channel)
        post: Post = await create_post(channel=channel, author=author, is_anonymous=True)

        resp = await client.get(
            settings.API_PATH + f"/posts/{post.id}", headers=get_jwt_header(author)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["author"]["id"] == str(author.id)


class TestReviewPost:
    async def test_forward_earns_token_reinjects_and_pops(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
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
        data = resp.json()
        assert data["reviewed_count"] == 1
        assert data["token_balance"] == 1  # earned one for reviewing

        await db.refresh(post)
        await db.refresh(user)
        assert post.forwarded_count == 1
        assert user.forwarded_count == 1
        # Popped from the queue, and re-injected as an operation (propagation).
        assert await service.render_queue_ids(redis, str(user.id), 10) == []
        assert await service.operation_queue_len(redis) == 1

    async def test_drop_earns_token_without_reinject(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "drop"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["token_balance"] == 1
        await db.refresh(post)
        assert post.dropped_count == 1
        assert await service.operation_queue_len(redis) == 0  # no re-injection

    async def test_review_not_in_queue_is_conflict(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)  # never placed in queue

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "not_in_queue"

    async def test_review_twice_second_is_conflict(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        first = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "forward"},
        )
        assert first.status_code == 200, first.text

        second = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "drop"},
        )
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "not_in_queue"

    async def test_review_nonexistent_post(self, client: AsyncClient, create_user):
        user: User = await create_user()
        resp = await client.post(
            settings.API_PATH + f"/posts/{10**6}/review",
            headers=get_jwt_header(user),
            json={"kind": "forward"},
        )
        assert resp.status_code == 404

    async def test_redelivery_after_review_is_already_reviewed_conflict(
        self,
        client: AsyncClient,
        redis: Redis,
        create_user,
        create_channel,
        create_post,
        monkeypatch,
    ):
        """The `post_reviews` unique constraint backstop (see CLAUDE.md /
        app.feed.service `FEED_EXCLUDE_SEEN`): normally `place_post` refuses to
        re-deliver a post the user's `seen` set already has, so this can only be
        reached if that guard is bypassed (e.g. an expired/lost seen set). Simulate
        that by disabling FEED_EXCLUDE_SEEN just for the re-delivery."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        first = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "forward"},
        )
        assert first.status_code == 200, first.text

        monkeypatch.setattr(settings, "FEED_EXCLUDE_SEEN", False)
        assert await service.place_post(redis, str(user.id), post.id) == 1
        monkeypatch.undo()

        second = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "drop"},
        )
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "already_reviewed"
        # Still removed from the queue by the failed attempt's claim.
        assert await service.render_queue_ids(redis, str(user.id), 10) == []


class TestDeliveryExclusions:
    async def test_author_never_receives_their_own_published_post(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel,
    ):
        """End to end: publish, then fan out the way the background consumer would.
        The author is the channel's only subscriber, so nobody gets it — and crucially
        the author's own feed stays empty."""
        author: User = await create_user()
        channel: Channel = await create_channel()
        await subscribe(db, author, channel)
        await service.sync_subscribe(redis, str(author.id), channel.id)
        await service.earn_token(redis, str(author.id), settings.FEED_PRICE_MAX)
        header = get_jwt_header(author)

        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=header,
            json={"channel_id": channel.id, "text": "mine"},
        )
        assert resp.status_code == 201, resp.text

        # The author rides along on the operation — that is the whole mechanism.
        entries = await redis.xrange(keys.STREAM)
        assert entries[0][1]["author_id"] == str(author.id)

        await consume_once(redis, "test-consumer", timeout=2.0)

        feed = await client.get(settings.API_PATH + "/posts/feed", headers=header)
        assert feed.status_code == 200, feed.text
        assert feed.json() == []

    async def test_forwarded_post_does_not_return_to_the_forwarder(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        """The loop that used to bite: forwarding re-injects the post, and before the
        seen set the forwarder was a perfectly valid recipient for it again."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        post: Post = await create_post(channel=channel)
        await subscribe(db, user, channel)
        await service.sync_subscribe(redis, str(user.id), channel.id)
        await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        resp = await client.post(
            settings.API_PATH + f"/posts/{post.id}/review",
            headers=header,
            json={"kind": "forward"},
        )
        assert resp.status_code == 200, resp.text

        await consume_once(redis, "test-consumer", timeout=2.0)

        feed = await client.get(settings.API_PATH + "/posts/feed", headers=header)
        assert feed.json() == []


class TestInteractionRateLimit:
    """The per-user budget shared by create/forward/drop (app/deps/rate_limit.py)."""

    async def test_review_burst_past_the_limit_is_throttled(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        user: User = await create_user()
        channel: Channel = await create_channel()
        limit = settings.INTERACTION_RATE_LIMIT
        posts = [await create_post(channel=channel) for _ in range(limit + 1)]
        for post in posts:
            await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        for post in posts[:limit]:
            resp = await client.post(
                settings.API_PATH + f"/posts/{post.id}/review",
                headers=header,
                json={"kind": "drop"},
            )
            assert resp.status_code == 200, resp.text

        resp = await client.post(
            settings.API_PATH + f"/posts/{posts[-1].id}/review",
            headers=header,
            json={"kind": "drop"},
        )
        assert resp.status_code == 429, resp.text
        body = resp.json()["detail"]
        assert body["error"] == "rate_limited"
        assert 1 <= body["retry_after"] <= settings.INTERACTION_RATE_WINDOW_SECONDS
        assert resp.headers["Retry-After"] == str(body["retry_after"])
        # Rejected before the handler ran: the post stays reviewable.
        assert await service.render_queue_ids(redis, str(user.id), 10) == [posts[-1].id]

    async def test_budget_is_shared_between_reviewing_and_posting(
        self, client: AsyncClient, redis: Redis, create_user, create_channel, create_post
    ):
        """Alternating between endpoints must not buy extra interactions."""
        user: User = await create_user()
        channel: Channel = await create_channel()
        limit = settings.INTERACTION_RATE_LIMIT
        posts = [await create_post(channel=channel) for _ in range(limit)]
        for post in posts:
            await service.place_post(redis, str(user.id), post.id)
        await service.earn_token(redis, str(user.id), settings.FEED_PRICE_MAX)
        header = get_jwt_header(user)

        for post in posts:
            resp = await client.post(
                settings.API_PATH + f"/posts/{post.id}/review",
                headers=header,
                json={"kind": "drop"},
            )
            assert resp.status_code == 200, resp.text

        balance_before = await service.token_balance(redis, str(user.id))
        resp = await client.post(
            settings.API_PATH + "/posts",
            headers=header,
            json={"channel_id": channel.id, "text": "one too many"},
        )
        assert resp.status_code == 429, resp.text
        # Throttled ahead of the handler, so nothing was charged for it.
        assert await service.token_balance(redis, str(user.id)) == balance_before

    async def test_superuser_is_exempt(
        self, client: AsyncClient, db: AsyncSession, redis: Redis,
        create_user, create_channel, create_post,
    ):
        user: User = await create_user()
        user.is_superuser = True
        db.add(user)
        await db.commit()
        channel: Channel = await create_channel()
        posts = [
            await create_post(channel=channel)
            for _ in range(settings.INTERACTION_RATE_LIMIT + 1)
        ]
        for post in posts:
            await service.place_post(redis, str(user.id), post.id)
        header = get_jwt_header(user)

        for post in posts:
            resp = await client.post(
                settings.API_PATH + f"/posts/{post.id}/review",
                headers=header,
                json={"kind": "drop"},
            )
            assert resp.status_code == 200, resp.text
